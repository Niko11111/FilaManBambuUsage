# 05 - Sources and evidence

Research as of **2026-08-19**. Every claim in the other documents can be traced
back to a file here. Doubt a claim, check it here rather than researching it
again.

---

## 1. Projects

| Project | Repository | License | Role here |
|---|---|---|---|
| FilaMan | `Fire-Devils/filaman-system` | MIT | target platform |
| Bambu Lab driver | `Fire-Devils/filaman-bambulab-plugin` | **no LICENSE** | read as a structural reference only |
| Spoolman API plugin | `Fire-Devils/filaman-spoolmanapi-plugin` | **no LICENSE** | reference for integration plugins |
| Bambuddy plugin | `Fire-Devils/filaman-bambuddy-plugin` | **no LICENSE** | read for comparison |
| OpenSpoolMan | `drndos/openspoolman` | MIT, (c) 2024 Filip Bednarik | origin of the consumption logic |
| Bambuddy | `bambuddy.cool` | AGPL-3.0 | alternative, not used |
| bambulabs_api | `BambuTools/bambulabs_api` | MIT, (c) 2023 Chris Ioannidis | MQTT and FTPS |
| BambuStudio | `bambulab/BambuStudio` | AGPL-3.0 | read to settle what `used_g` counts, nothing taken |

Without a LICENSE file, copyright applies undiminished and the code is all
rights reserved. **Reading is allowed, copying is not.** See `NOTICE`.

Open item: ask Fire-Devils whether the plugin repositories can get a license.

## 2. Evidence in FilaMan

Paths relative to `backend/` in `Fire-Devils/filaman-system`. The rows on the
lifecycle, the authentication dependencies and `record_consumption()` were
re-read against the source on 2026-08-20, the rest on 2026-08-18.

| Claim | Location |
|---|---|
| ZIP allow list without image formats, 10 MB limit | `app/services/plugin_service.py`, `ALLOWED_EXTENSIONS`, `MAX_ZIP_SIZE` |
| Required manifest fields, required files | `app/services/plugin_service.py`, `REQUIRED_MANIFEST_FIELDS`, `_validate_structure()` |
| `plugin_type` is `driver`, `import`, `integration` | `app/services/plugin_service.py`, `VALID_PLUGIN_TYPES` |
| `plugin_key` regex, semver | `app/services/plugin_service.py`, `PLUGIN_KEY_PATTERN`, `SEMVER_PATTERN` |
| Dependencies through uv, falling back to pip | `app/services/plugin_service.py`, `_install_dependencies()` |
| Subdirectories inside the package survive installation | `app/services/plugin_service.py`, `_extract_zip()` inspects only the top ZIP level, install uses `shutil.copytree` |
| Plugins live under `/app/data/plugins/<key>/` | `app/plugins/manager.py`, namespace comment at the top |
| Drivers are imported as `app.plugins.<key>.driver` | `app/plugins/manager.py`, `load_driver()` |
| Plugin routers are mounted at **module import time**, not in the lifespan | `app/main.py`, `mount_deferred_plugin_routers(app)` on the last lines of the file |
| An integration plugin exposes only `router` and `admin_router` | `app/api/v1/router.py`, `_mount_plugin_routers()` |
| A custom lifespan makes router `on_startup` handlers inert | `app/main.py`, `FastAPI(..., lifespan=lifespan)` |
| An import error in a plugin's `router.py` only logs a warning | `app/api/v1/router.py`, `_mount_plugin_routers()`, `except Exception` |
| `DBSession`, `PrincipalDep`, `RequirePermission(key)` returning `Depends(...)` | `app/api/deps.py` |
| CSRF is enforced for writes **only below `/api/v1/`** and for `/auth/logout` | `app/core/middleware.py`, `CsrfMiddleware.dispatch()` |
| A plugin's `admin_router` is mounted with `prefix="/api/v1"` | `app/api/v1/router.py`, `mount_deferred_plugin_routers()` |
| Four worker processes, so module state exists four times | `Dockerfile`, `CMD ["gunicorn", "-w", "4", ...]`; confirmed on the test instance, 38 of 100 health calls from a worker without its own setup |
| `AuthMiddleware` runs for every path, not only for `/api/`, and fills `request.state.principal` from the `session_id` cookie | `app/core/middleware.py`, `AuthMiddleware.dispatch()` |
| Permission keys read `<entity>:<action>` and are resolved against seeded roles | `app/api/v1/printers.py` and `spools.py`, `RequirePermission("printers:update")`; `app/api/deps.py`, `resolve_user_permissions()` |
| SQLite runs with `PRAGMA foreign_keys=ON`, so declared cascades fire | `app/core/database.py`, `_set_sqlite_pragmas()` |
| `record_consumption(spool, delta_weight_g, event_at, principal=None, source="ui", note=None)` | `app/services/spool_service.py` |
| The page is resolved at request time | `app/main.py`, `@app.get("/plugin-page/{plugin_slug:path}")` |
| Only drivers get a lifecycle | `app/main.py`, `plugin_manager.start_all()`; `app/plugins/manager.py`, `start_printer()` |
| Several workers, startup guarded by an exclusive `fcntl.flock`, with watchdog takeover | `app/main.py`, `_STARTUP_LOCK_PATH`, `lifespan()`, `_watchdog_try_takeover()` |
| The `BaseDriver` interface | `app/plugins/base.py` |
| The event bus carries thin notifications only | `app/core/event_bus.py`; `app/plugins/manager.py`, `event_bus.publish({"event": "slots_update", ...})` |
| `Printer.driver_config` holds the credentials | `app/models/printer.py` |
| `PrinterSlot.custom_fields["slot_index"]` | `app/models/printer.py`; `app/plugins/manager.py`, slot upsert |
| `PrinterSlotAssignment.spool_id` | `app/models/printer.py` |
| `record_consumption()` flips the sign, aggregates, clamps at 0 | `app/services/spool_service.py`, `record_consumption()` |
| The Spoolman layer can write today, `/use` included | `app/plugins/spoolmanapi/service.py`, `use_spool()`, `measure_spool()`, `update_spool()` |
| `get_all_settings()` returns `currency` only | `app/plugins/spoolmanapi/service.py`, `get_all_settings()` |
| `get_extra_fields()` and `add_extra_field()` are stubs | `app/plugins/spoolmanapi/service.py` |

### FilaMan's i18n contract

| Claim | Location |
|---|---|
| Language lives in `localStorage['lang']`, falls back to `en` | `frontend/src/lib/i18n.ts`, `initI18n()` |
| Synchronised from the user profile after `/api/v1/me` | `frontend/src/lib/i18n.ts`, `syncLangFromUser()` |
| Nested JSON, dotted keys, `{name}` interpolation | `frontend/src/lib/i18n.ts`, `resolve()` and `t()` |
| DOM translated through `data-i18n`, `data-i18n-placeholder`, `data-i18n-title` | `frontend/src/lib/i18n.ts`, `translatePage()` |
| Dictionaries as one JSON file per language | `frontend/src/i18n/en.json`, `de.json`, 22 top level namespaces |

This is the contract `bambu_usage/locales/` and `page.html` mirror, so the
plugin page follows the language selected in FilaMan without a switch of its
own.

### Reference plugin filaman-spoolmanapi-plugin

| Claim | Location |
|---|---|
| Integration manifest with `page_url` and `mount_prefix` | `spoolmanapi/plugin.json` |
| The page is a single HTML file, everything inline | `spoolmanapi/page.html`, about 22 KB, one `<style>`, two `<script>`, externally only `/favicon.png` and Google Fonts |
| Two routers side by side, one of them authenticated | `spoolmanapi/router.py` |
| An own table with private `MetaData`, `create(checkfirst=True)` | `spoolmanapi/settings.py` |
| Hard wired to German, no i18n | `spoolmanapi/page.html`, `lang="de"` and literal strings |

## 3. Evidence in OpenSpoolMan

| Claim | Location |
|---|---|
| Network print detected by `command == "project_file"` and `url` | `mqtt_bambulab.py`, `processMessage()` |
| Local print detected by `PREPARE` to `RUNNING`, 3MF from `gcode_file` | `mqtt_bambulab.py`, `processMessage()` |
| On a local print `ams_mapping` is empty | same file, `PENDING_PRINT_METADATA["ams_mapping"] = []` |
| The mapping is reconstructed from filament changes | `mqtt_bambulab.py`, `map_filament()` |
| Change detection over `stg_cur`, `mc_print_sub_stage`, `tray_tar` | `mqtt_bambulab.py`, the condition guarding the call to `map_filament()` |
| Deduction happens **at the start**, once the mapping is complete | `mqtt_bambulab.py`, `spendFilaments(PENDING_PRINT_METADATA)` on `complete` |
| Tray number to AMS and tray, `n // 4` | `spoolman_service.py`, `spendFilaments()`, `getAMSFromTray()` |
| External spool is AMS 255, tray 254 | `config.py`, `EXTERNAL_SPOOL_AMS_ID`, `EXTERNAL_SPOOL_ID` |
| 3MF over FTPS with pycurl, user `bblp` | `tools_3mf.py`, `download3mfFromFTP()`, `setupPycurlConnection()` |
| Only the file name is used, path components discarded | `tools_3mf.py`, comment "Pull just filename" |
| `slice_info.config`, `plate_<N>.png`, `plate_<N>.gcode` | `tools_3mf.py`, `getMetaDataFrom3mf()` |
| Filament number is 1-based, `ams_mapping` 0-based | `spoolman_service.py`, `ams_mapping[filamentId - 1]` |
| History across `prints`, `filament_usage`, `print_layer_tracking` | `print_history.py`, `create_database()` |
| Configuration through environment variables only, no interface | `config.env.template`, `config.py` |
| Mutable module level state driven from MQTT callbacks | `mqtt_bambulab.py`, `PRINTER_STATE`, `PENDING_PRINT_METADATA`; `spoolman_service.py`, `SPOOLS`, `SPOOLMAN_SETTINGS` |
| Spoolman endpoints used | `spoolman_client.py`: `fetchSpoolList`, `getSpoolById`, `patchExtraTags`, `consumeSpool`, `fetchSettings` |

The second to last row is the reason for the rule against module level mutable
state in `CLAUDE.md`. It is not a hypothetical concern.

## 4. Side finding on the SpoolmanScale documentation

`SpoolmanScale/Notes/FilaMan_Integration_Status.md` (as of 2026-04-30) records
that FilaMan's Spoolman compatibility layer is "GET only, no PATCH, unusable".
**That no longer holds.** `spoolmanapi/service.py` provides full CRUD today,
including `PUT /spool/{id}/use` and a genuine merge of `extra` into
`custom_fields`.

Verified on 2026-08-19 against the actual instance, not just against the source
on GitHub:

```
GET  http://192.168.4.100:8002/spoolman/api/v1/info     200
GET  http://192.168.4.100:8002/spoolman/api/v1/health   200  {"status":"healthy"}
```

The OpenAPI schema of that same instance lists, among 176 paths:

```
PUT  /spoolman/api/v1/spool/{spool_id}/use
PUT  /spoolman/api/v1/spool/{spool_id}/measure
```

That settles the GET only claim empirically.

Corrected along the way: the instance runs on **192.168.4.100:8002**, not on the
192.168.4.59 named in the older note.

Practical consequence, independent of this plugin: an existing OpenSpoolMan
instance can be pointed at FilaMan with very little work. Four of the five
functions in `spoolman_client.py` work unchanged. Only `fetchSettings()` breaks,
because it expects `data["extra_fields_spool"]["value"]` while FilaMan's
`get_all_settings()` returns `currency` alone. Roughly ten lines of defensive
parsing cover it.

That is the fastest way to try consumption tracking with FilaMan **before** this
plugin is finished. It does not replace it: an OpenSpoolMan instance still
serves exactly one printer and has no settings interface.

## 5. Not established, still open

These are plausible from the source but **not demonstrated in practice**. Stage
2 has to settle them first.

- How many uvicorn workers this instance runs, and therefore how often a
  request driven bootstrap happens. That an integration plugin has **no** startup
  hook at all is settled from the source as of 2026-08-20, see
  `02_FilaMan_Plugin_API.md` section 4.
- Whether `dependencies` in the manifest are installed for an integration plugin
  the same way as for a driver.
- Whether a newly mounted router really needs a restart while the page appears
  immediately.
- How many concurrent MQTT clients a Bambu printer in LAN mode tolerates.
  Several already run in this setup, but no reliable upper bound is known.
- Baseline before installation, measured on 2026-08-19: both
  `/plugin-page/bambu-usage` and `/bambu-usage/health` answer with 404, and the
  page reports `{"detail":"Plugin page not found"}`. The catch-all is therefore
  working and simply finds no matching plugin. That is the comparison value for
  acceptance.
