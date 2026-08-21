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
`plugin_manager.start_all()` starts drivers only, and only per printer. It gets
no startup hook either: FilaMan mounts the router while the module is imported,
before an event loop exists, and its own `lifespan` makes router `on_startup`
handlers inert.

The import itself is therefore the only hook, and there is no event loop at that
moment. The listeners consequently run in a daemon thread with a loop of their
own, started from `__init__.py`, and an exclusive `fcntl.flock` picks the single
worker of four that connects to the printers. Details in
`02_FilaMan_Plugin_API.md` section 4.

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

### 6.2 Which spool sits in the slot

Asked twice, in this order:

1. **FilaMan's `PrinterSlotAssignment`.** A person or the driver put that spool
   there, and nothing read off a printer overrules it.
2. **The RFID tag of the tray**, from `print.ams` in the report the listener
   receives anyway: `report.tray_tags()` reads it, `filaman.find_spool_by_rfid()`
   looks the spool up by `rfid_uid`, compared without regard to case.
3. Neither answers: the row stays open and says so. Never guessed.

The second step exists because of a measurement, not a preference. FilaMan's
Bambu Lab driver stores a tray's type, colour, `tray_info_idx` and temperatures,
but **not** its `tray_uuid`. The one identifier that connects a tray to the
spool in it never reaches the database, so the match cannot happen there, and
without step 2 every print would arrive with nothing assigned. Verified on a
running instance: a spool carrying its Bambu uuid in `rfid_uid`, the tray
holding it reported by the printer, and `spool_id: null` on every slot.

How a row came by its spool is recorded in `filament.spool_source`:
`filaman`, `tag` or `manual`. The card names the two that explain something,
"found by RFID" and "assigned by hand"; FilaMan's own assignment says nothing,
being the normal case.

Beside it sits a different fact, and keeping the two apart is the point: whether
somebody overruled the **amount**. That remark only appears where the booked
weight really differs from the estimate, because a correction that changed
nothing is not a message. It also quietly retires the rows from before 0.8.2,
when picking a spool set the same flag as correcting an amount.

**A limit worth knowing.** The match reads FilaMan's `rfid_uid`, and that field
holds whatever was put there. A scale connected to FilaMan writes the tag it
read, which for a third party spool is an NTAG the printer never reports. The
tag route therefore covers spools carrying their Bambu uuid, no more. FilaMan
can also assign a slot itself within a time window after weighing
(`auto_assign_enabled`, off by default); where that is used its assignment wins,
and ours never runs. **That combination is untested.**

We read the tag for our own booking and write nothing back.
`printer_slot_assignments` belongs to the driver, which rewrites it from the
printer's own reports; two writers on one row and the other one is faster.

An empty tray reports a uuid of nothing but zeros. It is never looked up, or
every empty chamber would hang on whichever spool happens to carry it.

### 6.3 Sequence, stage 1

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

### 6.4 Deducting at the end, not at the start

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

**An aborted print costs the share it got through.** Booking nothing leaves the
spool wrong after every abort, and booking the full estimate is what OpenSpoolMan
gets wrong.

The share is read **per filament out of the plate gcode**: the 3MF is opened at
print start, the gcode streamed, and for every layer the share of each filament's
material that has been laid down by then is recorded. Stopping at layer 20 then
costs exactly what layer 20 had used, and a filament that only appears in the
last third costs nothing at all. See `03_Bambu_Data_Sources.md` for what the
gcode looks like and why the curve is stored as shares rather than as amounts.

Where there is no curve, because the gcode was missing or unreadable, the linear
share falls back to `layer_num / total_layer_num` and then to `mc_percent`. Both
are approximations, a dense bottom layer weighs more than a sparse one in the
middle, and `mc_percent` measures time rather than material. Layers are preferred
for exactly that reason.

**A share that is unknown books nothing.** A printer that never reported its
progress leaves the row open for a correction rather than getting a made up
number, because an invented amount on a spool cannot be told apart from a real
one afterwards.

**A running print is never booked, by anybody.** Not automatically, not through
the button on the card, and not through assigning a spool by hand. It has laid
down a share whose end nobody knows yet, so charging the full estimate would put
material on the bill that is still on the spool. `spend_print` and
`correct_usage` both refuse while the status is `running`, because the answer
must not depend on which of the two asked.

That leaves one case the booking loop cannot see: a row booked while the print
was still running, through an older version or a direct API call, on a print
that is then cancelled. The loop only picks up rows without a `spent_at`, so
that row would keep the full estimate forever. `_rebook_stopped_rows` closes
it: every already booked row is recomputed from its **estimate** times the share
the print reached, and the difference is given back. A row corrected by hand is
left alone, because a number a person entered is not for a machine to overrule.

The same curve answers a second question while the print is still running: what
it has used **so far**. The page shows it per filament and as a total, computed
with the arithmetic the booking would use, so the figure grows towards exactly
what will be deducted at the end rather than towards a second opinion. It is
display only, nothing is ever booked from it, and without a curve it stays
empty for the reason above.

## 7. Failure cases

Each of these needs defined behaviour. Quietly doing nothing is not behaviour.

| Case | Behaviour |
|---|---|
| No `PrinterSlotAssignment.spool_id` on the slot | Print lands in the history, the row is marked "assignment open" and offers a dropdown to assign a spool. Do not guess. |
| **Every** slot without an assignment, on every print | Not a fault of this plugin and not repairable by it. FilaMan can only record an assignment once the printer has accepted it, and one such attempt was refused on the test instance; what it depends on is not established, see `05_Research_Sources.md`. The history stays usable: assign by hand, book, done. |
| External spool, `slot_index` `"255-254"` | Treated like any other slot. FilaMan keeps it as a regular slot. |
| Print aborted or failed | Do not deduct. Record the status, offer correction. |
| Stopped by a person against stopped by a fault | A fault leaves a code behind in `print_error` or `mc_print_error_code`, a print somebody cancelled does not. **This reading is an assumption**, taken from how the reports are commonly understood and not verified against a printer, so the code is stored with the print: the first real fault shows whether the rule stands the right way round. |
| 3MF unavailable (FTPS fails, file deleted) | Record the print with its file name and no consumption, status "3mf missing". Never drop it silently, or the print disappears without trace. |
| Printer offline or MQTT drops | The listener reports its state on the page and reconnects with backoff. Other printers keep running. |
| Plugin starts mid print | The beginning was missed and the mapping is unknown. The print is recorded as "incomplete" from the next state onwards and is not deducted. |
| Two slicer filaments on the same spool | Sum the consumption and deduct **once**, not twice. |
| Spool swapped while the print runs | The filament row is **split** where the change happened: the old spool keeps the share up to that point, the new one gets the rest. Nothing is split when the progress is unknown, and a slot that momentarily resolves to nothing is ignored, because a tray is briefly empty during a swap. See below. |
| The AMS switches to a **different tray** by itself when a spool runs empty | **Not detected.** The spool in the original tray does not change, so the comparison sees nothing. It would need `ams.tray_now`, a field `03_Bambu_Data_Sources.md` does not establish yet. Known limit, to be picked up once it is proven on a real printer. |
| Remaining weight would go negative | `record_consumption()` clamps at 0 itself. The plugin relies on that and does not do its own arithmetic. |
| Duplicate MQTT message | A print is identified by `subtask_id`, or by file name plus start time. Rows already deducted carry a flag and are never deducted twice. |

### 7.1 Why a swapped spool splits the row

A spool that runs empty is replaced, and from that moment the print draws from a
different one. Charging either spool for the whole print is wrong in one
direction or the other, and OpenSpoolMan cannot express the case at all.

The share comes from the same figure an aborted print is booked at, so there is
one notion of "how far did it get" and not two: the filament's own curve out of
the plate gcode where there is one, the linear share of the layers otherwise.

**The split produces two rows, not a table of segments.** Everything downstream
already works per row: the booking, the summing per spool, the correction by hand
and the display. A slicer filament that came off two spools genuinely is two
entries, and the two new columns `from_fraction` and `to_fraction` say which part
of the print each of them covers. Both empty means the whole print, which is the
normal case and shows nothing extra on the page.

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

**History** as the main view. One card per print: the preview image, the file
name and the outcome as a badge, a line of facts underneath, and then one block
per filament with its colour, its slot, the spool it was booked against and the
two weights.

The facts are printer, whether the job came from the cloud or from the printer
itself, when it ran, how long it took (or what the slicer predicted while it is
still running), how much material, how many layers, how many objects and which
nozzle.

Behind the duration sits how far it was off the prediction, in minutes and per
cent, red for slower and green for faster. Only for a print that ran to its end:
one stopped at a third would read "-70%" in green as though it had been fast.
The colour follows the percentage rather than the minutes, because half an hour
on a two day print is not a print that went wrong. Each carries a small monochrome icon. They live inline in `page.html` as
`<template>` elements, because the plugin ZIP rejects `.svg`, and every one of
them draws in `currentColor` so it takes the colour of whatever it sits next
to, in all three themes.

A print can also be taken out of the list, which asks first, in our own window
rather than through `confirm()`, because a browser dialog cannot be translated.
**What was booked stays booked.** The filament was really used, and letting a
spool refill itself through tidying up would make the history the enemy of the
stock. The dialog says so rather than leaving it to be discovered.

Above the list sits a search, a printer, a switch that leaves the failed ones
out, and a sort order. The search covers three things at once, because they are
three answers to one question and nobody should need a syntax to tell them
apart: the file name, a material, and a spool number (`25` or `#25`, and only
where the term really is a number). The spool **name** is deliberately not
searchable: it lives in FilaMan's tables, which `store.py` may not read, and
matching it on the page would only search the records already loaded.

The printer is a dropdown rather than a search term for the same reason. We
store `printer_id`, the name comes from FilaMan, and the page has the list
already. All three are answered by the query in `store.list_prints`
rather than by the page, because a filter that only searches the four records
already loaded is not a filter. The sort order is an allow list and the search
term is escaped, so neither can become a way to read something else.

**A fresh installation says what is missing.** FilaMan mounts plugin routers
when it starts, while the page itself comes from its catch-all, so right after
an upload the page loads and nothing under `/bambu-usage/` answers. That state is
recognisable rather than guessed: a path with no router behind it returns
`404 {"detail":"Not Found"}`, while an unreachable FilaMan fails the request
outright. The first case shows a notice naming the restart, the second keeps
saying the router is out of reach, because a restart would not be the advice to
give. On a 404 the page stops after the health check instead of letting every
other request add its own vague complaint, and when the routes appear it picks
the boot up by itself, without a reload.

While a print runs its card is fetched again on every poll, so what it has used
so far grows with it; the finished list below is left alone and keeps however
much of it is unfolded.

A block whose spool is still open is highlighted, because that is the only thing
the page ever asks of a human.

**While a print runs, the page says whether the spool will last.** The editor
names what the row costs, in grams and in metres, and beside every spool in the
list stands "not enough left" or "cutting it close" where its remaining weight
does not cover what is still to be laid down (the estimate minus what is already
down, not the whole print). The same marker sits on the card beside the spool
name, because that is where somebody sees it without opening anything, and
seeing it early is the entire point. On a finished print neither appears: a
warning nobody can act on is noise. Everything that can be changed about one filament
sits behind the pencil on its block and opens one window: the booked amount, and
the spool, chosen from a filtered list with colour and remaining weight rather
than from a dropdown. Changing the spool of a filament that was already booked
**moves** the booking, see `04_Data_Model.md`.

**Status** compactly above it: connected or not per printer, the running print,
the last error.

**Settings** per printer, with the counterparts to OpenSpoolMan's environment
variables:

| Setting | Counterpart | Default |
|---|---|---|
| Tracking enabled | none, new | on |
| Deduct automatically | `AUTO_SPEND` | on |
| Deduct on abort | none, new | on, and only the share that ran |
| ~~Clear assignment when empty~~ | `CLEAR_ASSIGNMENT_WHEN_EMPTY` | **not adopted**, see below |
| Keep history (days) | none, new | 365 |

`TRACK_LAYER_USAGE` is deliberately absent, it belongs to stage 4.

**`CLEAR_ASSIGNMENT_WHEN_EMPTY` is not adopted at all.** Clearing the link
between a tray and a spool means writing into FilaMan's
`printer_slot_assignments`, and that table belongs to the Bambu Lab driver, which
rewrites it from the printer's RFID data on the next report. Two writers on one
row, and the other one is faster. The switch existed in OpenSpoolMan because
Spoolman has no driver keeping that link current; FilaMan has one, so taking an
emptied spool out and putting a new one in updates the assignment by itself and
this plugin simply reads the result. See CONTRIBUTING.md section 3.

The column stays in `bambu_usage_settings`, because nothing in this schema is
ever dropped, and it is marked as abandoned there.

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

**Stage 4, done in part.** The cumulative extrusion per layer is read out of the
plate gcode and used for the two bookings that need a share: an aborted print and
a spool swapped mid print. What is still missing from the counterpart to
`TRACK_LAYER_USAGE` is showing a running print what it has used so far.

**Independently of all that**, three questions for Fire-Devils:

1. Whether the plugin repositories can get a license. Without one, nothing can
   be taken from them, see `NOTICE`.
2. Whether the Bambu Lab driver could publish a `print_complete` event on the
   event bus. If that succeeds, stage 2 loses its own MQTT connection and
   `tracker.py` shrinks considerably.
3. Whether a plugin page could be rendered inside FilaMan's own shell, so the
   navigation drawer stays visible. Today the navigation links straight at
   `page_url` and the backend returns `page.html` raw, which means a plugin page
   takes over the whole window. The clean fix is a route in the frontend that
   embeds the page in `Layout.astro`; the alternative, rebuilding the drawer
   inside `page.html`, would duplicate somebody else's interface and rot with
   every FilaMan release. Until then the page carries a link back.

## 11. Module layout

| Module | Responsibility | Knows MQTT | Knows HTTP |
|---|---|---|---|
| `report.py` | what one report from a printer means, purely functional | no | no |
| `tracker.py` | one listener per printer: the connection, and acting on what report.py decided | yes | no |
| `supervisor.py` | which worker runs the listeners, and keeping their list current | no | no |
| `threemf.py` | fetching and reading the 3MF, purely functional | no | no |
| `rules.py` | the arithmetic of consumption, purely functional | no | no |
| `service.py` | the booking path: recording a print, resolving slots, deducting | no | no |
| `views.py` | turning stored rows into what the page shows | no | no |
| `filaman.py` | the only module that reads FilaMan's own models and services | no | no |
| `store.py` | the queries on the plugin's own tables | no | no |
| `models.py` | the plugin's tables and their lifecycle | no | no |
| `settings.py` | loading and storing settings | no | no |
| `schemas.py` | Pydantic models for the router | no | no |
| `router.py` | endpoints, translations, thumbnails | no | yes |

Separating `tracker.py` from `service.py` is deliberate, see section 4: it is
the price of being able to move to the event bus later without a rebuild.
`supervisor.py` sits next to it for a different reason: how a listener is
started is a question about worker processes and lock files, and what a report
means is a question about printers. One file answering both could not be
described in a sentence.

**Three of these are pure**, and that is the point of them. `report.py`,
`rules.py` and `threemf.py` decide things without a database, a printer or
FilaMan, which is why they carry most of the tests and why the tests need no
setup at all. Everything that opens a session or a socket is somewhere else.

`filaman.py` exists for the same kind of reason. Every coupling to FilaMan's
internals, listed in `02_FilaMan_Plugin_API.md` section 8, lives in that one
file, so a FilaMan update breaks one module instead of five. It imports `app`
and SQLAlchemy inside the functions that need them, which keeps the pure parts
of this plugin importable without a running FilaMan. `router.py` is the single
exception to that rule, and a forced one: FastAPI resolves dependencies while
the route decorators run, so the authentication dependencies have to be there at
import time.

**Sessions are passed, never opened deep inside.** Every function that touches
the database takes an `AsyncSession` as its first argument. The router hands
down the session from FilaMan's `DBSession` dependency, the listeners open one
through `filaman.session_scope()`.

**The commits live in `service.py`, and that is forced.**
`SpoolService.record_consumption()` commits the session itself, on every path
through it, so nothing above it can own a transaction that spans a booking. The
consequences are worked out where they matter:

- A print is committed when it starts, so it survives a restart in the middle.
- The end status of a print is committed before anything is booked, so a failed
  booking cannot erase how the print ended.
- Inside a booking the filament rows are marked **before** the call, so the mark
  and the booking land in the same commit. A failure rolls back only that
  spool's marks, and everything already committed stays booked and is never
  booked twice, because a row carrying a `spent_at` is not picked up again.

A caller must therefore not wrap these functions in a transaction of its own.

These boundaries are not a matter of taste. `tools/check_architecture.py`
parses the imports and fails the build when one of them is crossed.
