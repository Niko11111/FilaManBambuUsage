# 02 - FilaMan's plugin system

Reference. Everything here was read out of the source of
`Fire-Devils/filaman-system` (MIT, as of 2026-08-18) and the reference plugin
`Fire-Devils/filaman-spoolmanapi-plugin`. Exact locations in
`05_Research_Sources.md`.

---

## 1. Installation and validation

Plugins are uploaded as a ZIP through Admin, Plugins.
`app/services/plugin_service.py` checks, in this order:

1. **Size:** `MAX_ZIP_SIZE = 10 * 1024 * 1024`, so 10 MB.
2. **ZIP integrity.**
3. **Extension allow list**, `ALLOWED_EXTENSIONS`:
   `.py .json .md .txt .cfg .ini .yaml .yml .toml .html`
   Anything else fails with `forbidden_extension`.
4. **Manifest** `plugin.json`, required fields
   `plugin_key`, `name`, `version`, `description`, `author`.
5. **Structure:** `plugin.json` and `__init__.py` are mandatory, plus
   `driver.py` when `plugin_type` is `driver`.
6. **Security check** over the contents.
7. **Driver check**, for type `driver` only.
8. **Dependencies** from `dependencies`, through `uv pip`, falling back to
   `sys.executable -m pip`.

Further rules:

- `plugin_key` against `^[a-z][a-z0-9_]{2,49}$`
- `version` against a simplified semver,
  `^\d+\.\d+\.\d+(-[a-zA-Z0-9.]+)?$`
- `plugin_type` out of `{"driver", "import", "integration"}`, defaulting to
  `driver`

Installation goes to `/app/data/plugins/<plugin_key>/`. That directory lives in
the data volume and survives container updates. Drivers are imported as
`app.plugins.<driver_key>.driver`, for which the manager grafts the plugin
folder into the `app.plugins` namespace.

Subdirectories inside the package are safe. `_extract_zip` only inspects the
top level of the ZIP to decide whether files sit at the root or in a single
folder, and installation copies with `shutil.copytree`. This is what allows
`locales/` to ship inside the package.

> **The consequence that matters most here:** no `.png`, `.css`, `.js` or
> `.svg` in the ZIP. The page is a single HTML file and images are served at
> runtime through a route. See `01_Design.md`, section 8.1.

## 2. Manifest

Fields the installer evaluates:

| Field | Required | Meaning |
|---|---|---|
| `plugin_key` | yes | unique key, and the folder name |
| `name` | yes | display name |
| `version` | yes | semver |
| `description` | yes | description in the admin view |
| `author` | yes | author |
| `plugin_type` | no | `driver`, `import`, `integration`. Defaults to `driver` |
| `driver_key` | for `driver` | module name under `app.plugins.<key>.driver` |
| `homepage` | no | link |
| `license` | no | license string |
| `page_url` | no | path of the plugin's page, conventionally `/plugin-page/<slug>` |
| `mount_prefix` | no | prefix the router is mounted under |
| `config_schema` | no | JSON Schema, FilaMan renders a form from it |
| `capabilities` | no | free form object describing what the plugin can do |
| `show_in_nav` | no | show an entry in the navigation, defaults to `false` |
| `dependencies` | no | list of pip requirements |
| `printer_params` | no | extra per printer fields, only meaningful for drivers |

## 3. Router and page

`app/main.py` calls `mount_deferred_plugin_routers(app)` at startup. That is
what mounts an integration plugin's router under its `mount_prefix`.

The page goes through a catch-all:

```python
@app.get("/plugin-page/{plugin_slug:path}")
async def serve_plugin_page(plugin_slug: str): ...
```

It searches the `plugin.json` of the installed plugins at request time for a
matching `page_url` and returns that plugin's `page.html`. **After installing,
no restart is needed for the page to appear.** A newly mounted router, on the
other hand, does need one, because mounting only happens at startup. That
difference is the thing that causes confusion while testing.

Because the page is served by FilaMan and not by the plugin, **the plugin
cannot template it**. Anything dynamic, translations included, has to be
fetched by the page at runtime from the plugin's own router.

The pattern from `spoolmanapi/router.py`, two routers side by side:

```python
# public, with its own access control, no login
router = APIRouter(prefix="/api/v1", dependencies=[Depends(require_ip_access)])

# administration, authenticated
admin_router = APIRouter(prefix="/admin/system/spoolman-api", tags=["admin-system"])
```

Authentication comes from `app.api.deps`: `DBSession` and `RequirePermission`.

## 4. Lifecycle, the trap

`plugin_manager.start_all()` starts **drivers only**, and does so per printer
through `start_printer(printer)`. An integration plugin receives **no** `start`
or `stop` call.

For this plugin that means the MQTT listeners have to start themselves. The way
to do it is an asyncio task launched when the router is imported and mounted,
with:

- idempotence, so several uvicorn workers do not each start their own set.
  FilaMan solves the same problem in `main.py` with a lock file under
  `tempfile.gettempdir()`, and that pattern can be reused.
- an independent reconnect per printer
- clean teardown when a printer disappears or the plugin is deactivated

Deactivated plugins are recorded in `InstalledPlugin` with `is_active = False`.
The manager skips printers whose `driver_key` belongs to a deactivated plugin.
This plugin has to observe its own state in the same way.

## 5. BaseDriver, for orientation

Not used here, but worth knowing. `app/plugins/base.py`:

```python
class BaseDriver(ABC):
    driver_key: str = ""
    def __init__(self, printer_id: int, config: dict, emitter: Callable[[dict], None]): ...
    @abstractmethod
    async def start(self) -> None: ...
    @abstractmethod
    async def stop(self) -> None: ...
    async def reconnect(self) -> None: ...
    def health(self) -> dict: ...
    def validate_config(self) -> None: ...
    def log_debug(self, direction: str, topic: str, payload) -> None: ...
```

`emitter` leads into the `PluginManager`, which writes `PrinterSlot` and
`PrinterSlotAssignment` from it and then publishes on the event bus.

## 6. Event bus

`app/core/event_bus.py`, in-process publish and subscribe with one
`asyncio.Queue` per subscriber, a queue depth of 64, and slow subscribers being
dropped.

```python
await event_bus.publish({"event": "slots_update", "printer_id": 1})
async for data in event_bus.subscribe(): ...
```

**Useless for consumption data**, because only thin notifications without
payload travel over it and the Bambu Lab driver feeds nothing suitable into it.
Useful for refreshing our own page, though: a `slots_update` is a good moment to
re-read the slot assignment.

## 7. Persisting settings

The pattern from `spoolmanapi/settings.py`, adopted here:

- a private `MetaData()` per plugin, so Alembic never touches the tables
- `Table("<plugin>_settings", _metadata, ...)`
- on first access, `await conn.run_sync(_table.create, checkfirst=True)`
- the result cached in module state
- `async_session_maker` and `engine` from `app.core.database`

The tables survive a ZIP update because they live in the database and not in
the plugin folder.

## 8. Internals this plugin uses

What this plugin imports from FilaMan. Every entry is a coupling that a FilaMan
update can break, which is why it is listed here rather than left implicit.

| Import | Purpose |
|---|---|
| `app.core.database.async_session_maker`, `engine` | database access |
| `app.core.event_bus.event_bus` | refreshing the page |
| `app.models.printer.Printer` | finding printers with `driver_key == "bambulab"`, reading `driver_config` |
| `app.models.printer.PrinterSlot` | `slot_no`, `custom_fields["slot_index"]` |
| `app.models.printer.PrinterSlotAssignment` | `spool_id` per slot |
| `app.models.spool.Spool` | loading a spool |
| `app.services.spool_service.SpoolService.record_consumption` | deducting |
| `app.api.deps.DBSession`, `RequirePermission` | securing endpoints |

## 9. Known uncertainties

- There is **no official plugin development guide**. The documentation on
  `docu.filaman.app` describes the bundled plugins from a user's point of view,
  not the interface. Everything here is derived from source.
- The Fire-Devils plugin repositories carry **no LICENSE file**. Reading them as
  a reference is fine, taking code is not. See `NOTICE`.
- Whether an integration plugin can reliably start a background task exactly
  once across all uvicorn workers is plausible from the source but **not
  demonstrated in practice**. That is the first thing stage 2 has to establish.
