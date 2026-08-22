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
3. **Per file, walking the extracted tree**, in this order:
   - **Extension allow list**, `ALLOWED_EXTENSIONS`:
     `.py .json .md .txt .cfg .ini .yaml .yml .toml .html`
     Anything else fails with `forbidden_extension`. A file without any
     extension passes, the check reads `if suffix and suffix not in ...`.
   - **1 MB per file**, `file_too_large`. Far below the 10 MB for the ZIP.
   - **No hidden file**, meaning no name starting with a dot, `hidden_file`.
     A `.DS_Store` that macOS wrote into the package folder is enough to have
     an upload refused, which is why `tools/build_zip.py` leaves the operating
     system's own files out while packing.
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
| `show_in_nav` | no | list the plugin in FilaMan's own navigation drawer, defaults to `false`. It adds the entry, nothing else, see section 3 |
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

**`show_in_nav` gets the plugin into the drawer, not the drawer onto the
plugin page.** `GET /api/v1/plugin-nav` (`app/api/v1/system.py`) returns the
active plugins that carry a `page_url` and have the flag set, cached for 600
seconds and invalidated whenever a plugin is installed, updated, toggled or
removed. `loadPluginNav()` in `frontend/src/layouts/Layout.astro` appends them
under a "Plugins" heading as `a.href = p.page_url`, a plain link. In FilaMan
since 1.1.6.

**A plugin page is still not embedded into FilaMan's interface.**
`serve_plugin_page()` answers with `FileResponse(page.html)`, so the browser
leaves the Astro shell and the drawer is gone for as long as the page is open.
The only plugin page that keeps it is the built-in FilamentDB import, whose
`page_url` is `/admin/system/filamentdb-import`, an Astro page of FilaMan's own.
No plugin can bring one: `frontend/astro.config.mjs` builds statically and nginx
serves the result from `/app/static`.

This plugin borrows the shell at runtime rather than copying it, and offers the
upstream fix as a pull request. See `01_Design.md` sections 8.3 and 10.

The pattern from `spoolmanapi/router.py`, two routers side by side:

```python
# public, with its own access control, no login
router = APIRouter(prefix="/api/v1", dependencies=[Depends(require_ip_access)])

# administration, authenticated
admin_router = APIRouter(prefix="/admin/system/spoolman-api", tags=["admin-system"])
```

Authentication comes from `app.api.deps`:

| Name | Shape | Use |
|---|---|---|
| `DBSession` | `Annotated[AsyncSession, Depends(get_db)]` | one session per request |
| `PrincipalDep` | `Annotated[Principal, Depends(require_auth)]` | any authenticated caller, which is what FilaMan's own read endpoints use |
| `RequirePermission(key)` | returns `Depends(...)`, so it fits a parameter default as well as the `dependencies=[...]` list | write endpoints |

Permission keys read `<entity>:<action>`, for example `printers:update`,
`spools:update`, `spool_events:create_consumption`. They are resolved against the
roles FilaMan seeds, so **a key a plugin invents belongs to no role** and would
lock out everyone except a superadmin. A plugin borrows an existing key instead
of making one up.

**Where a write endpoint lives decides whether it is CSRF protected.**
`CsrfMiddleware` in `app/core/middleware.py` checks `POST`, `PUT`, `PATCH` and
`DELETE` **only for paths below `/api/v1/`**, plus `/auth/logout`, and only for
session authenticated callers. A plugin that puts a write under its own
`mount_prefix` therefore creates the one state changing endpoint on the instance
that FilaMan's protection does not cover.

The way out is the second router FilaMan already reads off the module.
`_mount_plugin_routers()` picks up `admin_router` next to `router`, and
`mount_deferred_plugin_routers()` includes it with `prefix="/api/v1"`. Writes
placed there are guarded like every FilaMan write. Such a call has to carry the
`X-CSRF-Token` header matching the `csrf_token` cookie, which is readable from
JavaScript; `Layout.astro` does exactly that for logout.

## 4. Lifecycle, the trap

`plugin_manager.start_all()` starts **drivers only**, and does so per printer
through `start_printer(printer)`. An integration plugin receives **no** `start`
or `stop` call.

It gets no startup hook either. Three findings out of the source, and they
compound:

1. `_mount_plugin_routers()` at the bottom of `app/api/v1/router.py` imports
   `<plugin_key>.router` and reads exactly two attributes off the module,
   `router` and `admin_router`. Nothing else is ever looked at.
2. `main.py` calls `mount_deferred_plugin_routers(app)` at **module import
   time**, on the last lines of the file, not inside `lifespan`. No event loop
   is running at that moment, so `asyncio.create_task()` is not available.
3. `app = FastAPI(..., lifespan=lifespan)`. A custom lifespan replaces the
   default one, and the default one is what would have run `on_startup`
   handlers. Handlers a plugin router brings along are never called.

**The consequence:** the first request into the plugin's own router is the
earliest moment any code of this plugin can run. Whatever has to happen once,
the tables included, hangs off that.

For the MQTT listeners this left two routes, and the first installation settled
it. **Measured on the test instance:** 38 of 100 calls to `/bambu-usage/health`
came back from a worker that had not run its own one-time setup, and the image
starts `gunicorn -w 4`. Starting listeners on the first request would therefore
have made tracking depend on which of four processes a request happened to hit.

**Chosen: an own event loop in a daemon thread**, started while the package is
imported, plus an exclusive `fcntl.flock` on a lock file of our own that decides
which single worker connects. The other three keep checking and take over when
the owner dies. The price is a second SQLAlchemy engine, because a connection
pool belongs to the loop that created it; it is built from FilaMan's own
`settings.database_url` in `filaman.create_background_engine()`.

The rejected route, starting on the first request, remains cheap and honest and
would be the right answer in a single worker deployment.

Either way the listeners need:

- idempotence, so several uvicorn workers do not each start their own set.
  FilaMan solves the same problem in `main.py` with `fcntl.flock` on
  `Path(tempfile.gettempdir()) / "filaman-startup.lock"`, held for the lifetime
  of the primary worker, plus a watchdog through which a secondary worker takes
  over when the lock becomes free. The pattern is reusable with a lock file of
  our own.
- an independent reconnect per printer
- clean teardown when a printer disappears or the plugin is deactivated

Deactivated plugins are recorded in `InstalledPlugin` with `is_active = False`.
The manager skips printers whose `driver_key` belongs to a deactivated plugin.
This plugin has to observe its own state in the same way.

One more consequence of finding 1: `_mount_plugin_routers()` wraps the import in
`try/except Exception` and only logs a warning. **An import error in `router.py`
disables the plugin silently**, leaving one line in the FilaMan log and a 404 on
every endpoint. That is the first place to look when the plugin appears
installed but answers nothing.

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

**One part of that pattern is not adopted: the cache.** The image starts
`gunicorn -w 4`, so four processes serve requests behind nginx, each with its
own module state. Caching settings in one of them means three stale copies as
soon as a write lands anywhere, and the staleness is silent. Measured on the
test instance: 38 of 100 calls to `/bambu-usage/health` came back from a worker
that had not run its own one-time setup yet. This plugin therefore reads its
settings from the database every time.

## 8. Internals this plugin uses

What this plugin imports from FilaMan. Every entry is a coupling that a FilaMan
update can break, which is why it is listed here rather than left implicit.

**`bambu_usage/filaman.py` is this table in executable form.** Every line except
the last one lives in that module, and it imports them inside the function that
needs them, so a missing or moved internal surfaces as one named error instead
of an ImportError from somewhere deep in a callback. The last line is the
exception: FastAPI resolves dependencies while the route decorators run, so
`router.py` has to import them at module level.

| Import | Purpose |
|---|---|
| `app.core.database.async_session_maker`, `engine` | database access |
| `app.core.event_bus.event_bus` | refreshing the page |
| `app.models.printer.Printer` | finding printers with `driver_key == "bambulab"`, reading `driver_config` |
| `app.models.printer.PrinterSlot` | `slot_no`, `custom_fields["slot_index"]` |
| `app.models.printer.PrinterSlotAssignment` | `spool_id` per slot |
| `app.models.spool.Spool` | loading a spool, and its purchase price for what a print cost |
| `app.models.app_settings.AppSettings` | the currency code, so a cost can be labelled |
| `app.services.spool_service.SpoolService.record_consumption` | deducting |
| `app.api.deps.DBSession`, `RequirePermission` | securing endpoints |

## 9. Known uncertainties

- There is **no official plugin development guide**. The documentation on
  `docu.filaman.app` describes the bundled plugins from a user's point of view,
  not the interface. Everything here is derived from source.
- The Fire-Devils plugin repositories carry **no LICENSE file**. Reading them as
  a reference is fine, taking code is not. See `NOTICE`.
- **How many uvicorn workers a FilaMan instance actually runs**, and therefore
  how often a request driven bootstrap happens. That an integration plugin has
  no startup hook at all is settled, see section 4; how many processes have to
  cooperate is not.
