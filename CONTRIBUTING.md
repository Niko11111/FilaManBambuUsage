# Contributing

The conventions this repository is built on. They are not style preferences:
most of them exist because something went wrong once, and the reason is written
down with the rule so that nobody has to rediscover it.

Pull requests are welcome. If you want to change something structural, open an
issue first and describe the route you would take.

---

## 1. Build, check, test

```bash
python3 tools/build_zip.py             # build dist/bambu_usage-<version>.zip
python3 tools/build_zip.py --check     # validate only
python3 tools/build_zip.py --selftest  # prove the validation bites

python3 tools/check_architecture.py    # module boundaries hold
python3 -m unittest discover -s tests -t .  # unit tests
```

The ZIP is uploaded in FilaMan under Admin, Plugins. `build_zip.py` mirrors the
validation in FilaMan's `plugin_service.py`, so packaging mistakes surface at
build time instead of during an upload.

The test suite runs on the standard library alone, so it needs nothing
installed. pytest picks the same tests up if it is available. There are no
automated tests against a real printer; that part is verified against a running
FilaMan instance and an actual print job.

## 2. Hard rules from FilaMan's plugin system

Enforced in FilaMan's `plugin_service.py`. Break one and the installation is
rejected.

- **File extensions in the ZIP are an allow list:** `.py .json .md .txt .cfg
  .ini .yaml .yml .toml .html`. **No `.png`, `.css`, `.js`, `.svg`.** That is
  why `page.html` is a single file with inline `<style>` and `<script>`, why the
  icons are inline `<template>` elements, and why images only ever leave through
  a route at runtime. Translations do ship, because `.json` is allowed.
- Maximum ZIP size is 10 MB, and **1 MB for a single file**.
- **No hidden files.** A `.DS_Store` from a Finder window is enough to have the
  upload refused. `build_zip.py` leaves those out while packing and names them
  in its output; every other hidden file fails the build.
- Required files: `plugin.json` and `__init__.py`.
- `plugin_key` must match `^[a-z][a-z0-9_]{2,49}$`, `version` must be semver.
- An integration plugin gets **no `start` / `stop` lifecycle**. Only drivers do,
  and only per printer. Background work runs as a thread started when the router
  is mounted, and has to guard against several workers each starting their own.

## 3. Architecture decisions, settled

- **Do not rebuild what FilaMan already has.** AMS overview, slot assignment
  and auto matching belong to the Bambu Lab driver. This plugin **reads**
  `PrinterSlot` and `PrinterSlotAssignment`.
  - **One exception, and it was measured before it was taken.** The driver keeps
    a tray's type, colour and `tray_info_idx`, but not its `tray_uuid`, so the
    RFID tag a spool carries can never be matched against the tray it sits in,
    and every print arrives with nothing assigned. This plugin therefore reads
    the uuid out of the report it receives anyway and looks the spool up by
    `rfid_uid`. **Only for its own booking:** FilaMan's assignment wins wherever
    there is one, and nothing is ever written back into
    `printer_slot_assignments`, which the driver owns and rewrites.
- **Deduct through the internal service**, not through the Spoolman API:
  `SpoolService(db).record_consumption(...)`.
- **An own MQTT connection per printer.** The Bambu Lab driver parses only
  `print.ams` and `print.vt_tray` and feeds no print data into the event bus, so
  there is nothing to listen in on. The reasoning is in `docs/01_Design.md`.
- **Credentials come from `Printer.driver_config`**, never from an input form of
  our own. The user configures a printer exactly once.
- **The plugin's own tables use a private `MetaData`**, created with
  `create(checkfirst=True)`. Alembic never touches them and they survive an
  update.

## 4. Coding principles

> **The goal above all others: every change has to survive a human code audit.**
> Write so that an experienced developer can understand, review and safely
> change this code in six months. If something merely works but cannot be
> explained, it is not finished.

### Approaching a change

1. **Understand first, then change.** Read the affected code before touching it.
   Never patch blind or rebuild on suspicion.
2. **Be able to state the problem in three sentences.** What it should do, what
   can go wrong, how to check the result.
3. **Choose the smallest sensible change.** One step solves one problem. No
   drive-by edits to things that are not part of the task.
4. **Propose a larger rebuild before doing it.** Do not quietly refactor at
   scale.

### Structure

5. **Separable logic goes into its own file or function.** If a file cannot be
   described in *one* sentence, it is too big and gets split.
6. **One function, one job.** The name says what it does. If it does several
   things ("and" or "then" in the name), split it.
7. **Measurable size limits.** A module over 400 lines or a function over 50
   lines is not automatically wrong, but it needs a reason stated in the
   docstring.
8. **Respect the layers.** They are written down in `docs/01_Design.md` section
   11, declared in each module docstring, and checked by
   `tools/check_architecture.py`:
   - `tracker.py` is the only module that knows MQTT
   - `threemf.py` is pure, imports nothing else from the plugin
   - `service.py` holds the business logic and knows neither MQTT nor HTTP
   - `router.py` holds no business logic, only endpoints
   Crossing a boundary fails the check. If a boundary genuinely needs to move,
   move it in the design document and in the checker, deliberately.
9. **No mutable module level state.** Learned from OpenSpoolMan, where
   `PRINTER_STATE`, `PENDING_PRINT_METADATA` and `SPOOLS` are global dicts
   mutated from MQTT callbacks. That is what makes it untestable. State belongs
   on an object or in the database. Upper case module constants are fine.
10. **Parse at the boundary.** Raw dicts from MQTT, from the 3MF and from HTTP
    become dataclasses at the edge. No raw dict travels deeper.
11. **Do not repeat yourself.** The same logic never lives in two places.
12. **Rule of three.** Generalise on the third similar case. Not earlier, that
    is speculation. Not later, that is the refactor we are trying to avoid.

### Readability

13. **Speaking names, no abbreviation puzzles.** `latest_pickup_time`, not
    `lpt`.
14. **No magic numbers or strings.** Named constants carrying the unit, for
    example `RECONNECT_MAX_SECONDS = 300`.
15. **Comments explain the WHY, not the WHAT.** Comment the non-obvious and the
    deliberate decisions.
16. **Exit early instead of nesting deep.** Guard clauses up front.
17. **No dead code and no commented out leftovers.** That is what the history is
    for.
18. **Type annotations on every public function.** A leading underscore marks
    something as internal.

### Robustness

19. **Guard every external call.** Every MQTT, FTPS, HTTP and database call can
    fail. Always `try/except` with a comprehensible message and a log entry.
    **No bare `except:`.** One error class per module.
20. **Never trust input or foreign data.** A printer can report nonsense.
21. **One printer must never take the others down.** Every listener owns its
    error state.
22. **Do not break what exists**, especially the booking path and data
    integrity. After a change, verify the old path still works.
23. **Few, well maintained libraries.** For a handful of lines, write it rather
    than adding a dependency. Pin versions, and make a library upgrade its own
    commit.

### Testability is a design constraint

24. **What cannot be called without a printer, without FilaMan and without a
    database will get rewritten later.** Pure logic stays pure and callable:
    `threemf.parse()` takes a path, `rules.tray_to_slot_index()` takes an int.
25. **A new pure function comes with a test.** `tests/` sits outside
    `bambu_usage/` and is not shipped.

### Documentation

26. **Specification and code change in the same commit.** `docs/` is the
    specification, not a session note.

## 5. Python and FastAPI

- **Syntax check before saving any `.py` file:**
  `python3 -c "import ast; ast.parse(open('file.py').read()); print('OK')"`
- Define all routes **before** any `if __name__` block, later ones are ignored.
- `FileResponse` and raw `Response` always carry an explicit `media_type`.
  Without it the thumbnail endpoint returns a 404 that looks like a routing bug.
- **Secrets never in code.** They come from `Printer.driver_config` or from the
  plugin's settings table.
- **Table changes are additive only.** New column, new table, never overwrite or
  drop an existing one. A new column has to be nullable, because a `NOT NULL`
  one would need a value for every row that already exists, and inventing one is
  how history gets corrupted. `ensure_tables()` has to stay safe to call
  repeatedly.
- Prefer `pathlib` over string paths, and `datetime` with an explicit timezone
  over naive timestamps.

## 6. The plugin page

- `page.html` is **one self contained file.** Inline `<style>` and `<script>`,
  no external assets beyond `/favicon.png` and Google Fonts. This is forced by
  the ZIP allow list, not a preference.
- **All colours come from the FilaMan theme tokens**, never hardcoded hex
  values. The three themes `brand`, `light` and `dark` all have to work.
- The page follows the theme through `data-theme` plus `localStorage['theme']`,
  exactly as the main application does.
- Anything dynamic is fetched from our own router. FilaMan serves the page
  through its catch-all, so it cannot be templated server side.

## 7. Language and translations

- **Everything in this repository is English.** Code, comments, documentation,
  interface text, commit messages, file names.
- **No em-dashes (U+2014) anywhere.** Use " - " or restructure the sentence.
  English prose invites them, which is exactly why the rule is written down.
- **No user facing strings in code**, neither in Python nor in HTML. Everything
  goes through a key in `bambu_usage/locales/`.
- `en.json` is the reference and is always complete. Other languages fall back
  to English key by key, so a missing translation shows English rather than a
  blank.
- Adding a language means adding one file. If it needs a code change, the
  mechanism is wrong.
- The API returns **stable error codes**, not prose, so the page can translate
  them. An English fallback message travels alongside so `curl` stays readable.
- `tests/test_i18n.py` checks that every language covers the keys in `en.json`,
  that no key is defined and unused, and that every marked attribute is one
  `translatePage()` actually handles.

### The German the plugin speaks

**The plugin lives inside FilaMan, so it speaks FilaMan's German.** Where FilaMan
has already chosen a word, that word wins: Spule, Drucker, Restmenge, Filament,
Slot.

Terms of the trade keep their English form, because that is what the field says
and a translation would only sound foreign:

| Kept | Not this |
|---|---|
| Layer | Schicht |
| Tracking, tracken | Erfassung, erfassen |
| Slot, Plate, Slicer, Filament, 3MF, AMS | any translation |
| External Tray | externes Fach |

**The plugin's own name is not translated.** The page heading reads "Bambu Usage
Tracking", the same as the entry in FilaMan's sidebar.

Bookkeeping stays German: abbuchen, Buchung, gebucht. That is the language of
keeping books rather than of 3D printing, and keeping books is what this plugin
does.

A further language follows the same line: take the host application's
vocabulary, keep the field's English terms, translate the rest.

## 8. Versioning

The version lives in exactly two places, and they must agree:

1. `bambu_usage/plugin.json`, field `version`
2. `bambu_usage/__init__.py`, `__version__`

`build_zip.py` refuses to build when they diverge, and it refuses to overwrite an
existing ZIP of the same version. Two different packages under one version
number make an instance report a version that no longer describes what is
installed, and the difference is invisible from outside. Use `--force` only for
a build that never left your machine.

Bump the PATCH level as soon as a change produces a new installable package,
meaning anything under `bambu_usage/`. Pure documentation or test changes that do
not alter the package do not need a bump.

## 9. Git

- `main` is stable. Develop on `dev` or `feature/xyz`, never directly on `main`.
- Conventional Commits (`feat:`, `fix:`, `docs:`), English.
- **Few, meaningful commits.** One topic, one commit, with a message describing
  the outcome rather than the path.
- **Always squash when merging into `main`.** A feature branch may carry
  intermediate commits as a safety net, `main` gets one commit per topic.
- `dist/` is never committed.

## 10. Legal

The consumption logic is ported from **OpenSpoolMan (MIT)**. The attribution
lives in [`NOTICE`](NOTICE) and has to be kept current as more is ported.

**No code is taken from the FilaMan plugin repositories.** They carry no LICENSE
file and are therefore all rights reserved. Reading them as a structural
reference is fine, copying is not.

## 11. Before calling anything done

- Would an outside developer understand this change in review and wave it
  through?
- Does every function do exactly one thing and is it named accordingly?
- Are all new failure modes caught, with no bare `except`?
- Did I break anything that existed, especially the booking path?
- `python3 tools/check_architecture.py` green?
- `python3 -m unittest discover -s tests -t .` green?
- `python3 tools/build_zip.py --selftest` green?
- Version bumped, if the package changed?
- No secrets, no magic numbers, no dead code, no em-dashes?
- Is any new user facing string a key rather than a literal?
- Does the documentation still match what the code does?
