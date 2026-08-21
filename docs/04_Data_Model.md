# 04 - Data model

What the plugin stores itself, and what it reads out of FilaMan.

---

## 1. Principle

**The plugin stores nothing twice.** Spools, filaments, printers and slot
assignments belong to FilaMan. The plugin stores only what FilaMan does not
know: print jobs, consumption per slot, and its own settings.

Its own tables follow the pattern from `spoolmanapi/settings.py`:

- a private `MetaData()`, so Alembic never sees them and a FilaMan update never
  trips over them
- created on first access with `create(checkfirst=True)`
- prefixed `bambu_usage_`, so origin and ownership are obvious
- **no real foreign keys into FilaMan's tables.** `printer_id` and `spool_id`
  are plain integers. A real foreign key would either block deleting a spool or
  silently take history with it. History should outlive the spool it refers to.

Because the tables live in the database and not in the plugin folder, they
survive a ZIP update. Uninstalling the plugin leaves them in place, and that is
intended: a later reinstall finds the history again.

**Surviving an update means more than creating what is missing.**
`create_all(checkfirst=True)` only ever creates whole tables, so a column added
in a later version of this plugin would simply be absent on an instance that
already ran an earlier one, and every query naming it would fail. `ensure_tables`
therefore also compares the columns and adds what is missing. Only nullable
columns can be added that way, which is what the additive-only rule amounts to
in practice: a NOT NULL column would need a value for every row that already
exists, and inventing one is how history gets corrupted. Such a column is
refused loudly instead.

## 2. Read from FilaMan

From `app/models/printer.py` and `app/models/spool.py`, read only.

### printers

| Field | Use |
|---|---|
| `id` | reference in the plugin's own tables |
| `name` | display |
| `driver_key` | filter for `"bambulab"` |
| `driver_config` | JSON with `host`, `serial`, `access_code`, `printer_model` |
| `is_active`, `deleted_at` | skip inactive and deleted printers |

### printer_slots

| Field | Use |
|---|---|
| `id`, `printer_id`, `slot_no` | identifying the slot |
| `custom_fields["slot_index"]` | `"<ams_id>-<tray_id>"`, the bridge to the tray number from `ams_mapping` |

### printer_slot_assignments

| Field | Use |
|---|---|
| `slot_id` | reference to the slot |
| `spool_id` | **which FilaMan spool sits in the tray.** The core of the mapping |
| `present` | whether anything is loaded at all |
| `meta` | `tray_type`, `tray_color`, `tray_info_idx`, for plausibility checks |

### spools

Only ever **written** through `SpoolService`, never directly. Three fields are
read on top of that, for what a print cost:

| Field | Use |
|---|---|
| `purchase_price` | what the spool cost |
| `initial_total_weight_g` | gross weight when it was new |
| `empty_spool_weight_g` | the empty spool |

The price of one gram is `purchase_price / (initial_total_weight_g -
empty_spool_weight_g)`. That net weight is FilaMan's own arithmetic, the figure
`SpoolService.rebuild_remaining_weight()` starts a spool at, so both agree on
what "a gram of filament" means.

A spool missing any of the three gets no price and is left out of the total. The
filament price is deliberately not used as a fallback: what it refers to is not
established, and a made up amount of money next to a real one is worse than a
gap.

## 3. Deducting

```python
from app.services.spool_service import SpoolService

await SpoolService(db).record_consumption(
    spool,
    delta_weight_g=grams,        # pass a positive value, the service flips the sign
    event_at=finished_at,
    source="bambu_usage",
    note=f"{file_name} ({printer_name})",
)
```

The service handles all of the following itself:

- the sign: a positive value becomes a deduction
- writing a `SpoolEvent` with `meta`, which is the audit trail
- aggregating events from the same source that fall close together, recording
  `aggregation_count` and `first_event_at` in `meta`
- carrying `remaining_weight_g` forward and clamping it at 0
- **committing the session**, on every path through the call

That last one is not a detail. It fixes the transaction boundaries for everything
that books, which is why the commits of this plugin sit in `service.py` and are
ordered the way `01_Design.md` section 11 describes.

A correction downwards cannot go through this call at all: a positive value is
turned into a deduction, so material is only ever taken away. Giving some back
is `record_adjustment(spool, "relative", delta_weight_g=...)`, where a positive
delta raises the remaining weight.

**Every timestamp leaves the plugin with its offset.** The columns are declared
`DateTime(timezone=True)`, but SQLite hands back what it was given without an
offset it never stored, so the values come out naive. Serialised like that, a
browser reads them as local time and shows a print an hour or two early.
`schemas.UtcDatetime` stamps a naive value as UTC on its way out, which is
correct rather than convenient: everything written here comes from
`datetime.now(timezone.utc)`.

Deleting a print never touches a spool. `store.delete_print` removes the print
and its filament rows and nothing else, the same way `purge_expired_history`
does it for the retention: rows first and explicitly, because the declared
cascade only fires where the database enforces foreign keys.

The same pair moves a booking from one spool to another, which is what happens
when a spool is corrected after the print was already booked: the new one is
charged and the old one credited, both with a note saying where it went. The
charge goes first on purpose, so a failure in between counts material twice
rather than conjuring it out of nowhere.

**The plugin recomputes none of this.** It passes grams and a timestamp and
relies on the service. `source="bambu_usage"` makes the origin visible in
FilaMan's spool log and separates it from the scale and from manual entry.

## 4. The plugin's own tables

### bambu_usage_settings

One row per printer, plus a global row with `printer_id = 0`.

| Column | Type | Default | Meaning |
|---|---|---|---|
| `printer_id` | int, PK | | `0` means global |
| `tracking_enabled` | bool | `true` | start a listener at all |
| `auto_spend` | bool | `true` | deduct automatically. Counterpart to `AUTO_SPEND` |
| `spend_on_cancel` | bool | `true` | deduct an abort as well, at the share it got through, see `01_Design.md` 6.4 |
| `clear_assignment_when_empty` | bool | `false` | **abandoned**, never read. It would mean writing into the driver's table, see `01_Design.md` 8.2. Kept because nothing here is dropped |
| `history_retention_days` | int | `365` | `0` means unlimited |
| `updated_at` | datetime | | |

With `auto_spend` off, the print is recorded in full but not deducted. The
history then offers a button to deduct it. This is the mode for watching along
before trusting the plugin with the spools.

### bambu_usage_prints

One print job.

| Column | Type | Meaning |
|---|---|---|
| `id` | int, PK | |
| `printer_id` | int | no foreign key, see section 1 |
| `subtask_id` | str, nullable | job identifier from the printer |
| `file_name` | str | display name |
| `print_type` | str | `cloud`, `local` |
| `plate_id` | int, nullable | plate from `slice_info.config` |
| `started_at` | datetime | |
| `finished_at` | datetime, nullable | |
| `status` | str | `running`, `finished`, `failed`, `cancelled`, `incomplete`, `no_3mf` |
| `completed_fraction` | float, nullable | how far a print that was stopped got, `0.0` to `1.0`. `NULL` means unknown, and then nothing is booked |
| `layer_shares` | text, nullable | JSON, per filament the share of its material each layer has used. The shape of the curve out of the plate gcode, never an amount, see `03_Bambu_Data_Sources.md` |
| `stopped_at_layer` | int, nullable | which layer the printer was on when it stopped, so the curve can be read at the right place long afterwards |
| `spent` | bool | whether it has already been deducted |
| `printer_error_code` | int, nullable | what the printer reported when the print ended. Absent or zero means nobody's machine broke, which is what tells cancelled from failed |
| `estimated_seconds` | int, nullable | what the slicer predicted for the plate, `prediction` in slice_info.config |
| `object_count` | int, nullable | how many objects sit on the plate |
| `nozzle_diameter` | float, nullable | the nozzle it was sliced for. A dual nozzle machine reports two, the first is kept |
| `thumbnail` | blob, nullable | `Metadata/plate_<N>.png` |
| `thumbnail_mime` | str, nullable | in practice `image/png` |
| `error` | str, nullable | last error concerning this print |
| `created_at` | datetime | |

Uniqueness on `(printer_id, subtask_id)` where a `subtask_id` exists, otherwise
on `(printer_id, file_name, started_at)`. A duplicate MQTT message therefore
creates no second print.

The image lives in the table as a BLOB. The reasoning is in `01_Design.md`
section 8.1: no assumption about writable paths, and FilaMan's backup covers the
database anyway. Typical size is in the low hundreds of kilobytes, and
`history_retention_days` bounds the growth.

### bambu_usage_filament

One row per slicer filament of one print. This is where the actual work happens.

| Column | Type | Meaning |
|---|---|---|
| `id` | int, PK | |
| `print_id` | int, FK to `bambu_usage_prints`, cascading | |
| `filament_id` | int | slicer filament number, **1-based as in the 3MF** |
| `slot_index` | str, nullable | `"<ams_id>-<tray_id>"`, `"255-254"` for external |
| `spool_id` | int, nullable | the resolved FilaMan spool. `NULL` means assignment open |
| `material` | str, nullable | from the 3MF |
| `color_hex` | str, nullable | from the 3MF |
| `tray_info_idx` | str, nullable | Bambu material identifier, for plausibility checks |
| `estimated_grams` | float, nullable | the slicer estimate. Never modified by a booking, but divided when a row is split |
| `estimated_length_m` | float, nullable | divided in the same proportion |
| `from_fraction` | float, nullable | from which share of the print this row applies |
| `to_fraction` | float, nullable | up to which. Both `NULL` means the whole print |
| `spent_grams` | float, nullable | what was actually booked |
| `spent_at` | datetime, nullable | |
| `manual_override` | bool | assignment or amount corrected by hand |

`estimated_grams` and `spent_grams` stay separate. The estimate is the source
value and is never overwritten, `spent_grams` is the booking. Only that way does
a correction stay traceable and comparable against the scale.

`spool_id` being `NULL` is the normal outcome for a local print in stage 1, and
what triggers the highlight in the history.

**One slicer filament can occupy more than one row.** When the spool in a tray is
swapped while the print runs, the row is split at the progress reached: the first
keeps `from_fraction` empty and gets `to_fraction`, the second starts there and
runs to the end. Their estimates are the original one divided in that proportion,
so the sum over the rows of a `filament_id` is still what the slicer predicted.
The reasoning is in `01_Design.md` section 7.1.

### bambu_usage_printer_status

Live state, not history. One row per watched printer, always current, never
accumulating.

| Column | Type | Meaning |
|---|---|---|
| `printer_id` | int, PK | |
| `printer_name` | str, nullable | shown on the page without a second lookup |
| `connected` | bool | whether the listener has an MQTT connection |
| `tracking_enabled` | bool | a printer with tracking off still gets a row, so it is visible as off rather than absent |
| `current_print_id` | int, nullable | the running print |
| `current_file_name` | str, nullable | |
| `progress_percent` | int, nullable | from `mc_percent` |
| `last_error` | str, nullable | the last failure concerning this printer |
| `updated_at` | datetime | how fresh the row is |

**Why a table and not memory.** The listeners run in one worker process while
the plugin page is answered by any of the four, so nothing kept in memory would
be visible to the request that has to render it. FilaMan solves the same problem
for its drivers with shared memory (`shared_health_store`); a table is simpler
here, survives a restart and is readable with `sqlite3` when something is wrong.

The row is written when a listener connects, disconnects, starts or ends a
print, fails, and once per reconcile round as a heartbeat. Not on every MQTT
message: a printer reports about once a second, and none of that is worth a
write.

## 5. The resolution chain in full

```
slice_info.config: filament id = 2, used_g = 41.2
        |
        | ams_mapping[2 - 1]  ->  5          global tray number, 1-based to 0-based
        v
ams_id = 5 // 4 = 1 , tray_id = 5 % 4 = 1
        |
        v
slot_index = "1-1"
        |
        | printer_slots WHERE printer_id = ? AND custom_fields->>'slot_index' = '1-1'
        v
PrinterSlot.id
        |
        | printer_slot_assignments WHERE slot_id = ?
        v
spool_id = 17
        |
        v
SpoolService.record_consumption(spool 17, 41.2 g, source="bambu_usage")
```

If the chain breaks anywhere, the row is stored with `spool_id = NULL` and
offered for correction in the history. **Do not guess.**

## 6. Several filaments on one spool

A print can address the same tray more than once, for instance a multi colour
model reusing the same colour in several places. Several rows in
`bambu_usage_filament` then point at the same `spool_id`.

**Before deducting, the amounts are summed per `spool_id` and booked once**, not
per row. Otherwise several events hang off the same spool and the aggregation
inside `record_consumption()` blurs which print cost what.

`spent_grams` is still carried per row, so the history shows the breakdown. What
gets booked is the sum.
