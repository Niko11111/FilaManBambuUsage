# 01 - Design

As of 2026-08-19, milestone 1. This document is the specification. Code follows
it. Changing the design means changing this document first.

---

## 1. Problem

FilaMan's Bambu Lab driver (`Fire-Devils/filaman-bambulab-plugin`, `driver.py`)
processes exactly two things out of the MQTT payload in `_process_slots()`:
`print.ams` and `print.vt_tray`. Everything else is discarded: `gcode_state`,
`subtask_name`, `ams_mapping`, `stg_cur`, `mc_percent`. The driver sees AMS
slots, but never a print job.

The consequence is that FilaMan knows which spool sits in which tray, but never
how much of it was used. Remaining weight has to be maintained by hand or
re-measured on the scale.

OpenSpoolMan solves this for Spoolman. This plugin brings the idea to FilaMan.

## 2. Scope

The most important design decision is **how little** the plugin does.

| Capability | Who provides it | This plugin |
|---|---|---|
| AMS overview, slots, humidity, temperature | Bambu Lab driver | reads only |
| Assigning a spool to a tray | Bambu Lab driver plus FilaMan UI | reads only |
| Manual assignment when detection fails | FilaMan UI, slot dots on the spool detail page | reads only |
| RFID, auto matching | Bambu Lab driver plus FilaMan | reads only |
| Printer records, credentials | FilaMan, `Printer.driver_config` | reads only |
| **Deducting consumption** | nobody | **this plugin** |
| **Print history** | nobody | **this plugin** |
| **Correcting an assignment after the fact** | nobody | **this plugin** |

OpenSpoolMan builds the top five rows itself, because Spoolman does not have
them. In FilaMan that would be duplicated work and would create two competing
sources of truth.

**Consequence for the user interface:** the plugin gets no AMS view. It gets a
print history and a settings page. Assigning a spool happens where FilaMan
already offers it.

## 3. Why integration and not driver

FilaMan knows three plugin types: `driver`, `import`, `integration`.

`driver` is out. A driver is attached to exactly one `Printer` record, and a
printer has exactly one `driver_key`. If this plugin were a driver, the user
would have to choose between the Bambu Lab driver and consumption tracking.
That is precisely what must not happen, both are meant to run side by side.

`integration` fits: an own FastAPI router, an own page, no interference with
printer management.

**The price:** an integration plugin gets **no `start` / `stop` lifecycle**.
`plugin_manager.start_all()` starts drivers only, and only per printer. The
MQTT listeners therefore have to start themselves as asyncio tasks when the
router is mounted, and clean up after themselves. Details in
`02_FilaMan_Plugin_API.md`.

## 4. Why an own MQTT connection

The obvious idea is to listen in on the existing Bambu Lab driver instead of
opening a second connection to the printer. That does not work today.

`app/core/event_bus.py` is an in-process publish and subscribe bus for SSE.
`subscribe()` is generic, so an integration plugin in the same process could
listen in. But the bus only carries thin notifications of the form
`{"event": "slots_update", "printer_id": 1}`, without payload. And the Bambu Lab
driver never feeds print data into it, because it does not parse it. What is
never parsed cannot be forwarded.

Three routes, assessed:

1. **An own MQTT connection.** Chosen. Bambu printers in LAN mode tolerate
   several concurrent clients, and this setup already runs several. No
   dependency on changes in someone else's repository.
2. **An upstream pull request against the Bambu Lab driver** that publishes a
   `print_complete` event with payload on the bus. Architecturally the clean
   answer, and the plugin would become a thin consumer with no connection of
   its own. Blocked on the FilaMan plugin repositories carrying no LICENSE
   file. **Worth asking Fire-Devils about**, independently of route 1 being
   built.
3. **Forking the Bambu Lab driver.** Ruled out for the same licensing reason.

The design keeps route 2 open: the consumption calculation lives in
`service.py` and knows nothing about MQTT. `tracker.py` is the only module that
knows where events come from. Switching to the event bus replaces `tracker.py`
and nothing else.

## 5. Multiple printers

This falls out almost for free, and is the clearest advantage over
OpenSpoolMan. There, one printer is hard wired through the `PRINTER_ID`,
`PRINTER_IP` and `PRINTER_ACCESS_CODE` environment variables, and more than one
printer means more than one instance.

Here:

```
SELECT * FROM printers WHERE driver_key = 'bambulab' AND is_active AND deleted_at IS NULL
```

One listener per match. Host, serial and access code live in
`Printer.driver_config`, the JSON column the Bambu Lab driver fills through its
`config_schema`. **The plugin never asks for credentials.** The user configures
a printer once in FilaMan, and the plugin finds it.

An unreachable printer must not take the others down with it. Every listener
runs in its own task with its own reconnect and its own error state, visible on
the plugin page.

The printer list changes at runtime. The plugin reconciles against it
periodically and starts or stops listeners accordingly, rather than relying on
a restart.

## 6. The course of a print

### 6.1 Two sources, unequal data

This is the central finding from analysing OpenSpoolMan's `mqtt_bambulab.py`,
and it determines how the stages are cut.

**Cloud and network prints** (sent from the slicer or the app): the message
carries `print.command == "project_file"` and `print.url`. That means
**`print.ams_mapping` is available ready made**, a list naming the global tray
number for each slicer filament. Clean, deterministic, very little code.

**Local prints** (started from the printer display or from SD): `print_type ==
"local"`, recognised by `gcode_state` moving from `PREPARE` to `RUNNING`. The
3MF sits on the printer as `print.gcode_file`. **There is no `ams_mapping`.**
OpenSpoolMan reconstructs it during the print by counting filament changes and
matching them against the filament order read from the plate gcode. Detecting a
change there rests on a heuristic over `stg_cur == 4`, transitions in
`mc_print_sub_stage`, `tray_tar` being `254` or `255`, and the special case of
`stg_cur` 13 followed by 24.

That heuristic is the most fragile part of OpenSpoolMan and the most likely
thing to break on a firmware change.

**Decision:** stage 1 supports **cloud and network prints only**. Local prints
are detected, recorded in the history with their preview and file name, and
marked as "assignment open" so the user can assign them by hand. The heuristic
lands in stage 3 at the earliest, and then as a clearly separated module that
can be switched off.

That is more honest than a heuristic that quietly gets it wrong, and it
delivers most of the value for very little code.

### 6.2 Sequence, stage 1

```
1. MQTT message with print.command == "project_file" and print.url
     -> a print is starting

2. Fetch the 3MF
     cloud URL      -> direct download
     otherwise      -> FTPS from the printer, bambulabs_api PrinterFTPClient

3. Read the 3MF (a ZIP)
     Metadata/slice_info.config   -> plate id, and per filament used_g, used_m,
                                     tray_info_idx, colour
     Metadata/plate_<N>.png       -> preview image
     Metadata/plate_<N>.gcode     -> filament order, only needed from stage 2 on

4. Record the print
     bambu_usage_prints with status "running", plus one row per filament in
     bambu_usage_filament carrying the estimate

5. Resolve slots, per slicer filament
     global tray number from print.ams_mapping
       ams_id   = tray // 4
       tray_id  = tray %  4
       slot_index = "<ams_id>-<tray_id>"      external spool: "255-254"
     find PrinterSlot through custom_fields["slot_index"]
     read PrinterSlotAssignment.spool_id

6. End of print, from gcode_state
     FINISH  -> deduct
     FAILED  -> do not deduct, status "failed", offer correction
     abort   -> same as FAILED

7. Deduct, per spool, with the total of every slot assigned to it
     SpoolService(db).record_consumption(
         spool, delta_weight_g=grams, event_at=now,
         source="bambu_usage", note="<file name>")
```

### 6.3 Deducting at the end, not at the start

OpenSpoolMan deducts **when the print starts**, as soon as the mapping is
complete (`spendFilaments()` runs once `PENDING_PRINT_METADATA["complete"]` is
set). An aborted print costs the full estimate there.

This plugin deducts on `FINISH`. The reasons:

- An abort does not consume the full amount. This is the most common case where
  OpenSpoolMan gets it wrong.
- The consumption carries the timestamp of the end of the print, which matches
  how the history is sorted.

The price is holding state across the duration of the print. That state
therefore lives in the database, not in memory: if FilaMan restarts mid print,
the plugin finds the open print again and can close it on the next `FINISH`.

Deducting a proportion of the estimate on abort, using `mc_percent`, is
conceivable. It is deliberately **not** part of stage 1, because percentage
progress is not linear in material used. The aborted print lands in the history
with its estimate and a correction field instead.

## 7. Failure cases

Each of these needs defined behaviour. Quietly doing nothing is not behaviour.

| Case | Behaviour |
|---|---|
| No `PrinterSlotAssignment.spool_id` on the slot | Print lands in the history, the row is marked "assignment open" and offers a dropdown to assign a spool. Do not guess. |
| External spool, `slot_index` `"255-254"` | Treated like any other slot. FilaMan keeps it as a regular slot. |
| Print aborted or failed | Do not deduct. Record the status, offer correction. |
| 3MF unavailable (FTPS fails, file deleted) | Record the print with its file name and no consumption, status "3mf missing". Never drop it silently, or the print disappears without trace. |
| Printer offline or MQTT drops | The listener reports its state on the page and reconnects with backoff. Other printers keep running. |
| Plugin starts mid print | The beginning was missed and the mapping is unknown. The print is recorded as "incomplete" from the next state onwards and is not deducted. |
| Two slicer filaments on the same spool | Sum the consumption and deduct **once**, not twice. |
| Spool swapped between start and end of the print | The assignment **at the end** is binding, because that is when the deduction happens. The history makes it visible when the assignment at start and end disagree. |
| Remaining weight would go negative | `record_consumption()` clamps at 0 itself. The plugin relies on that and does not do its own arithmetic. |
| Duplicate MQTT message | A print is identified by `subtask_id`, or by file name plus start time. Rows already deducted carry a flag and are never deducted twice. |

## 8. User interface

### 8.1 The asset constraint

`plugin_service.py` checks every file in the ZIP against an extension allow
list: `.py .json .md .txt .cfg .ini .yaml .yml .toml .html`. **No `.png`,
`.css`, `.js`, `.svg`.** This is not a recommendation, it is a hard rejection at
install time.

That forces two things:

- `page.html` is **a single file** with inline `<style>` and `<script>`. The
  reference, `spoolmanapi/page.html`, does exactly that in 22 KB: its own theme
  tokens, `data-theme` plus localStorage as in the main application, and
  externally nothing but `/favicon.png` and Google Fonts.
- **Plate previews cannot be shipped.** They are runtime data anyway. They live
  as a BLOB in `bambu_usage_prints` and are served through
  `GET <mount_prefix>/thumb/{print_id}`.

BLOB rather than file, because the plugin should make no assumption about
writable paths inside the container, and because FilaMan's backup covers the
database regardless.

Translations are the exception that the allow list permits: `.json` is allowed,
so the dictionaries ship inside the package. See section 9.

### 8.2 What the page shows

**History** as the main view: per print a preview image, file name, printer,
timestamp, status, and per slot the consumption together with the spool it was
booked against. Rows with an open assignment are highlighted and carry a
dropdown of spools, so the correction happens where the problem is visible.

**Status** compactly above it: connected or not per printer, the running print,
the last error.

**Settings** per printer, with the counterparts to OpenSpoolMan's environment
variables:

| Setting | Counterpart | Default |
|---|---|---|
| Tracking enabled | none, new | on |
| Deduct automatically | `AUTO_SPEND` | on |
| Deduct on abort | none, new | off |
| Clear assignment when empty | `CLEAR_ASSIGNMENT_WHEN_EMPTY` | off |
| Keep history (days) | none, new | 365 |

`TRACK_LAYER_USAGE` is deliberately absent, it belongs to stage 4.

## 9. Languages

English is the reference language. Further languages can be added without
touching code.

The page is served by FilaMan's catch-all at `/plugin-page/{slug}`, not by our
router, so strings cannot be substituted server side. The page has to fetch
them at runtime.

We mirror FilaMan's own contract, taken from `frontend/src/lib/i18n.ts`:

- the chosen language lives in **`localStorage['lang']`**, synchronised from the
  user profile through `/api/v1/me`, falling back to `en`
- nested JSON with **dotted keys**, interpolation through **`{name}`**
- the DOM is translated through **`data-i18n`**, `data-i18n-placeholder` and
  `data-i18n-title`
- `lang` is set on the document element

Because we use the same contract, **the plugin page follows whatever language
the user selected in FilaMan.** No separate switch, no second setting.

```
bambu_usage/locales/en.json      reference, always complete
bambu_usage/locales/de.json      German
```

Served through `GET <mount_prefix>/i18n/{lang}`, falling back to `en` for an
unknown language. Adding a language means adding one file.

Two rules follow for the code:

- **No user facing strings in code**, neither in Python nor in HTML. Everything
  goes through a key.
- The API returns **stable error codes**, not prose, so the page can translate
  them. An English fallback message travels alongside so that a raw `curl` stays
  readable.

## 10. Stages

**Stage 1, this milestone.** Specification, scaffold, a buildable and
installable package with a placeholder page and the translation mechanism in
place. No tracking.

**Stage 2.** A listener per printer, fetching and reading the 3MF, deducting
cloud and network prints, the plugin's own tables, history with previews,
assignment after the fact.

**Stage 3.** Local prints: the filament change heuristic as a module that can be
switched off, filament order from the plate gcode.

**Stage 4, open.** Per layer tracking as the counterpart to
`TRACK_LAYER_USAGE`, proportional deduction on abort.

**Independently of all that:** ask Fire-Devils whether the plugin repositories
can get a license, and whether the Bambu Lab driver could publish a
`print_complete` event on the event bus. If that succeeds, stage 2 loses its own
MQTT connection and `tracker.py` shrinks considerably.

## 11. Module layout

| Module | Responsibility | Knows MQTT | Knows HTTP |
|---|---|---|---|
| `tracker.py` | listener per printer, state machine, reconciling the printer list | yes | no |
| `threemf.py` | fetching and reading the 3MF, purely functional | no | no |
| `service.py` | resolving slots, computing consumption, deducting | no | no |
| `models.py` | the plugin's tables | no | no |
| `settings.py` | loading and storing settings | no | no |
| `schemas.py` | Pydantic models for the router | no | no |
| `router.py` | endpoints, translations, thumbnails | no | yes |

Separating `tracker.py` from `service.py` is deliberate, see section 4: it is
the price of being able to move to the event bus later without a rebuild.

These boundaries are not a matter of taste. `tools/check_architecture.py`
parses the imports and fails the build when one of them is crossed.
