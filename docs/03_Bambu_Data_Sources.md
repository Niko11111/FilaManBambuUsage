# 03 - Data sources on the Bambu printer

Reference for the two sources this plugin draws from: the MQTT state and the
3MF file. Read out of OpenSpoolMan's `mqtt_bambulab.py`, `tools_3mf.py` and
`spoolman_service.py` (MIT), and out of `bambulabs_api` (MIT).

---

## 1. Access

Everything runs over the local network, no Bambu cloud.

| | Value |
|---|---|
| MQTT | TLS, port 8883, user `bblp`, password is the access code |
| FTPS | port 990, explicit, user `bblp`, password is the access code |
| Topic | `device/<serial>/report` |

Host, serial and access code sit in `Printer.driver_config`, filled by the
Bambu Lab driver through its `config_schema` (`host`, `serial`, `access_code`,
`printer_model`). **The plugin does not ask for them again.**

### Why bambulabs_api and not pycurl

OpenSpoolMan builds its FTPS connection with pycurl (`FTP_SSL = FTPSSL_ALL`,
`FTPSSLAUTH = FTPAUTH_TLS`). pycurl is a C extension and needs a compiler and
`libcurl-dev` inside the container. Installed through `dependencies` in the
manifest, that would mean a build on every plugin installation, with the
failure modes that implies.

`bambulabs_api` (MIT, 2.6.6) depends only on `paho-mqtt` and `pillow`, both
available as wheels, and provides both halves:

```python
from bambulabs_api import Printer
p = Printer(ip_address, access_code, serial)
p.ftp_client          # PrinterFTPClient: listing, download, images
p.get_state()         # GcodeState
p.get_file_name()
p.get_percentage()
```

An additional benefit: FilaMan's Bambu Lab driver already declares
`bambulabs_api>=2.6.0` as a dependency. With both plugins installed, the library
is there regardless.

**Two things about `PrinterFTPClient` this plugin works around**, read out of
`bambulabs_api/ftp_client.py`:

- `download_file()` collects the whole archive into a `BytesIO`, so a 3MF of
  tens of megabytes lands in the worker's memory.
- Its `connect_and_run` decorator wraps the call in `except Exception`, logs the
  failure and returns `None`. A refused connection is therefore
  indistinguishable from an empty file, and a print would be booked at zero
  instead of recorded as `no_3mf`.

`threemf.py` uses the class for its implicit FTPS handshake, which is the part
worth having, and drives `connect`, `login` and `retrbinary` itself, streaming
to disk. It also sets the socket timeout the library leaves unset, which
otherwise means "block forever" on a silent printer.

## 2. MQTT state

The printer sends partial updates. The state has to be carried along and
merged, because a single message is rarely complete. OpenSpoolMan keeps
`PRINTER_STATE` and `PRINTER_STATE_LAST` for this and compares transitions.

> Note for our implementation: OpenSpoolMan keeps both as module level globals
> mutated from MQTT callbacks. That is exactly what this project forbids, see
> `CLAUDE.md`. The same state belongs on the listener object.

### Relevant fields under `print`

| Field | Meaning | Needed for |
|---|---|---|
| `command` | `project_file` marks a submitted print job | print start, network print |
| `url` | source of the 3MF on a cloud print | fetching the 3MF |
| `gcode_state` | `IDLE`, `PREPARE`, `RUNNING`, `PAUSE`, `FINISH`, `FAILED` | start and end |
| `print_type` | `local` or `cloud` | which path applies |
| `gcode_file` | path of the 3MF on the printer | fetching the 3MF over FTPS |
| `subtask_name` | job name | display |
| `subtask_id` | job identifier | making a print unique |
| `ams_mapping` | list, the global tray number per slicer filament | **the core of the mapping** |
| `mc_percent` | progress in percent | display, later the abort proportion |
| `layer_num`, `total_layer_num` | layer progress | stage 4 |
| `ams.tray_tar` | target tray on a change, `254` external, `255` back into the AMS | stage 3 |
| `stg_cur`, `mc_print_sub_stage` | stage in the sequence, `4` is a filament change | stage 3 |
| `ams`, `vt_tray` | AMS slots and external spool | used by the Bambu Lab driver, not needed here |

### ams_mapping and the tray numbers

`ams_mapping` is a list over the slicer filaments. The value is a **global** tray
number across all AMS units, and `-1` means unused.

```
"ams_mapping": [1, 0, -1, -1, -1, 1, 0]
```

The conversion, as OpenSpoolMan does it in `spendFilaments()`
(`getAMSFromTray(n) = n // 4`):

```
ams_id  = tray // 4
tray_id = tray %  4
```

FilaMan stores the same thing as a string in
`PrinterSlot.custom_fields["slot_index"]`, formatted `"<ams_id>-<tray_id>"`.
That gives the bridge:

```python
EXTERNAL_SLOT_INDEX = "255-254"     # external spool, AMS 255, tray 254

def tray_to_slot_index(tray: int) -> str:
    if tray < 0:
        return EXTERNAL_SLOT_INDEX
    return f"{tray // 4}-{tray % 4}"
```

The constants `255` and `254` come from OpenSpoolMan's `config.py`
(`EXTERNAL_SPOOL_AMS_ID`, `EXTERNAL_SPOOL_ID`, marked "don't change" there) and
agree with what FilaMan's Bambu Lab driver checks for the external slot in
`health()`.

### Detecting the start of a print

**Network and cloud prints.** `print.command == "project_file"` with `print.url`
set. `ams_mapping` is present. If it is missing, the external spool is meant.

**Local prints.** `print_type == "local"` and `gcode_state` moving from
`PREPARE` to `RUNNING`. The 3MF is named in `gcode_file` and has to be fetched
over FTPS. **There is no `ams_mapping`.**

For the local case OpenSpoolMan reconstructs the mapping during the print: it
counts filament changes and assigns each newly targeted tray the next filament
from the order found in the plate gcode. A change is considered detected on
`stg_cur == 4` with certain preceding states, on `mc_print_sub_stage` moving
from `4` to `2`, on `tray_tar == "254"`, or on `stg_cur` `13` followed by `24`.

**That heuristic is the most fragile part of OpenSpoolMan.** It depends on
undocumented state numbers in the printer firmware. This is why local prints are
deferred to stage 3 here, see `01_Design.md` section 6.1.

## 3. The 3MF file

A 3MF is a ZIP. Three entries are of interest.

### Metadata/slice_info.config

XML. Provides, per plate, the filaments with their estimated consumption. This
is the number that gets deducted.

```xml
<plate>
  <metadata key="index" value="1"/>
  <filament id="1" type="PLA" color="#FFFFFF" used_m="12.34" used_g="41.2"
            tray_info_idx="GFA00"/>
</plate>
```

| Attribute | Meaning |
|---|---|
| `id` | slicer filament number, **1-based**. `ams_mapping` is 0-based, so `ams_mapping[id - 1]` |
| `type` | material |
| `color` | colour in the slicer |
| `used_g` | estimated consumption in grams |
| `used_m` | estimated length in metres |
| `tray_info_idx` | Bambu material identifier, for example `GFA00` |

The 1-based `id` against the 0-based index is a classic off-by-one.
OpenSpoolMan writes `ams_mapping[filamentId - 1]` for it.

#### What used_g counts, and what it cannot

**The purge is already in it.** Read out of BambuStudio (AGPL-3.0, nothing
taken, as of 2026-08-20):

- `PlateData::parse_filament_info()` in `src/libslic3r/Format/bbs_3mf.cpp`
  derives `used_g` from `print_statistics.total_volumes_per_extruder`.
- `GCodeProcessor::UsedFilaments::update_flush_per_filament()` in
  `src/libslic3r/GCode/GCodeProcessor.cpp` adds the flushed volume into exactly
  that total, not only into `flush_per_filament`.
- The wipe tower lands in the same total through `increase_wipe_tower_caches()`.
- `m_result.print_statistics.total_volumes_per_extruder =
  m_used_filaments.total_volumes_per_filament` closes the chain.

So the estimate covers the model, the support, the wipe tower and the material
flushed on a filament change. The flush is even split between the outgoing and
the incoming filament, by how much of the old one was still sitting in the
nozzle. **This plugin therefore books the purge automatically and must never add
anything on top of `used_g`.**

What the estimate cannot cover is everything the printer extrudes outside the
sliced job: loading or unloading a filament from the AMS menu, a calibration
line, a purge after a change made by hand. None of that appears in the 3MF or in
any MQTT field this plugin reads, so booking it would mean inventing a constant
and guessing which spool to charge. Deliberately out of scope, see
`01_Design.md` section 10.

### Metadata/plate_<N>.png

The plate preview. `<N>` is the plate id from `slice_info.config`. This is
exactly the image OpenSpoolMan shows in its history, and the answer to the
question of how gcode files are represented: it comes out of the 3MF, not out of
the gcode.

It goes into `bambu_usage_prints` as a BLOB, because the ZIP may not carry image
files. See `01_Design.md` section 8.1.

### Metadata/plate_<N>.gcode

The actual gcode. The only thing needed from it is the order in which the
filaments appear, and that only for local prints in stage 3. Stage 1 never opens
the file.

The file is large. It must never be read into memory as a whole, only line by
line out of the open ZIP entry.

## 4. What this means for fetching

- The 3MF is fetched and read **once per print**, never repeatedly.
- The download needs a timeout and must not block the listener.
- If it fails, the print is still recorded, with status "3mf missing". A print
  that vanishes without trace is worse than one without numbers.
- The printer holds several files with similar names. When fetching over FTPS,
  OpenSpoolMan deliberately takes only the file name and discards path
  components, so as not to reach into other directories. Worth keeping.
