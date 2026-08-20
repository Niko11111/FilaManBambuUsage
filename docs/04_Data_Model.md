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

Only ever touched through `SpoolService`, never written directly.

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
| `spend_on_cancel` | bool | `false` | deduct on abort as well |
| `clear_assignment_when_empty` | bool | `false` | counterpart to `CLEAR_ASSIGNMENT_WHEN_EMPTY` |
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
| `spent` | bool | whether it has already been deducted |
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
| `estimated_grams` | float, nullable | the slicer estimate, never modified |
| `estimated_length_m` | float, nullable | |
| `spent_grams` | float, nullable | what was actually booked |
| `spent_at` | datetime, nullable | |
| `manual_override` | bool | assignment or amount corrected by hand |

`estimated_grams` and `spent_grams` stay separate. The estimate is the source
value and is never overwritten, `spent_grams` is the booking. Only that way does
a correction stay traceable and comparable against the scale.

`spool_id` being `NULL` is the normal outcome for a local print in stage 1, and
what triggers the highlight in the history.

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
