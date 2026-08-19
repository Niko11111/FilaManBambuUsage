# FilaManBambuUsage

A plugin for [FilaMan](https://github.com/Fire-Devils/filaman-system) that
deducts the filament used by a Bambu Lab print from the matching FilaMan spool,
automatically.

> **Status: milestone 1, specification and scaffold.**
> The plugin builds and installs, but tracks nothing yet. The Python modules are
> stubs. The full design is in [`docs/01_Design.md`](docs/01_Design.md).

## Why

FilaMan's Bambu Lab driver reads only `print.ams` and `print.vt_tray` out of the
MQTT payload, which is the AMS slot state and nothing else. It discards
`gcode_state`, `subtask` and `ams_mapping`, so it never learns about print jobs
and never deducts anything.

For Spoolman, [OpenSpoolMan](https://github.com/drndos/openspoolman) solves
exactly this. This plugin brings the idea to FilaMan, with three differences:

| | OpenSpoolMan | this plugin |
|---|---|---|
| Printers | exactly one, hard wired through environment variables | any number, taken from FilaMan's printer list |
| Configuration | eleven environment variables, no interface | a page in FilaMan, credentials reused from the existing printer |
| Runtime | its own container next to Spoolman | runs inside FilaMan |

## What it will do

- Attach over MQTT to every printer whose `driver_key` is `bambulab`
- Detect the start and the end of a print from `gcode_state`
- Fetch the 3MF from the printer over FTPS and read `used_g` per filament
- Map slicer slots to physical AMS trays through `ams_mapping`, and from there
  to the FilaMan spool through `PrinterSlotAssignment`
- Deduct when the print finishes, through `SpoolService.record_consumption()`
- Show a print history with plate previews, consumption per slot, and a way to
  correct an assignment after the fact

## What it deliberately does not do

AMS overview, assigning a spool to a tray, reading RFID, auto matching.
FilaMan's Bambu Lab driver already does all of that, better than a
reimplementation could. This plugin only reads that state.

## Building

```bash
python3 tools/build_zip.py            # -> dist/bambu_usage-0.1.0.zip
python3 tools/build_zip.py --check    # validate only
python3 tools/build_zip.py --selftest # prove the validation bites
```

The build mirrors FilaMan's own checks: extension allow list, size limit,
required files, manifest schema. What passes here is accepted by FilaMan.

```bash
python3 tools/check_architecture.py   # module boundaries hold
python3 -m unittest discover -s tests -t .   # unit tests, stdlib only
```

## Installing

1. In FilaMan, go to **Admin, Plugins**
2. Upload `dist/bambu_usage-0.1.0.zip`
3. **Bambu Usage** appears in the navigation

No restart is needed for the page, FilaMan resolves plugin pages per request.
The router is mounted at startup, so a first installation does need one restart
before the endpoints answer.

## Requirements

- FilaMan with support for `plugin_type: "integration"`
- At least one printer with `driver_key == "bambulab"`, which is where host,
  serial and access code come from
- The printer reachable on the LAN over MQTT and FTPS. The Bambu cloud is not
  used

## Languages

The interface ships in English and German and follows whatever language is
selected in FilaMan, because it reads the same `localStorage['lang']` the main
application uses. There is no separate switch.

Adding a language means adding one file under `bambu_usage/locales/`, for
example `fr.json`, using `en.json` as the template. No HTML and no Python needs
to change. Missing keys fall back to English rather than leaving a blank.

## License

MIT, see [`LICENSE`](LICENSE). Ported parts and their origin are recorded in
[`NOTICE`](NOTICE).

## Documentation

| Document | Contents |
|---|---|
| [`docs/01_Design.md`](docs/01_Design.md) | design, sequence, failure cases, decisions |
| [`docs/02_FilaMan_Plugin_API.md`](docs/02_FilaMan_Plugin_API.md) | how FilaMan's plugin system works |
| [`docs/03_Bambu_Data_Sources.md`](docs/03_Bambu_Data_Sources.md) | MQTT fields and the structure of the 3MF |
| [`docs/04_Data_Model.md`](docs/04_Data_Model.md) | the plugin's tables and the FilaMan models it reads |
| [`docs/05_Research_Sources.md`](docs/05_Research_Sources.md) | evidence, by repository, file and location |

## Contributing

Pull requests are welcome. Please read [`CLAUDE.md`](CLAUDE.md) first: it holds
the conventions this repository is built on, including the module boundaries
that `tools/check_architecture.py` enforces.
