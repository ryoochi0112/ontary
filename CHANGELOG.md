# Changelog

Notable changes to `ontary`. Format loosely follows
[Keep a Changelog](https://keepachangelog.com/); versioning follows
[docs/compatibility.md](docs/compatibility.md) — pre-1.0, so **a minor bump may break
you**.

`ontary` continues `ontos`, authored at Atrae, Inc. and released to the author on
2026-09-06 for open-source development under MIT. Entries below `[0.10.0]` describe
`ontos` releases; their tags exist here as renamed snapshots of the same source.

## [Unreleased]

### Added

- **Parameter `description` (#65).** `ActionParameterDef` gains an optional
  `description` (`str | None`). MCP `list_action_types` and `list_functions` publish it
  on every parameter object, right after `type`, and it is `null` when undescribed.
- **`MISSING_DESCRIPTION` advisory lint (#65).** `diagnose()` and `ontary validate`
  warn about an action or Function whose description is blank or only the default
  "Executes X." / "Computes X.". Docstrings never count. `accept="MISSING_DESCRIPTION"`
  silences it.
- **`RESULT_NOT_JSON` error code (#167).** A Function or Action result that cannot cross
  the JSON boundary is refused with this `precondition` code. The message names the
  `api_name` and the key path of the first offending value.

### Changed

- **Function and Action results encode `date` and `datetime` as ISO 8601 strings (breaking, #167).**
  In-process `call_function` now returns the strings the store keeps, where it used to
  return `datetime` and `date` objects. MCP `call_function` and an Action result no
  longer fail with `INTERNAL_ERROR` on these types.
- **A result that is not JSON refuses with `RESULT_NOT_JSON` (breaking, #167).**
  Both surfaces refuse with this `precondition` code instead of `INTERNAL_ERROR`,
  including an Action that returns a non-`dict`. In-process `call_function` also
  refuses non-JSON values it used to return unchanged: tuples, sets, `Decimal`, model
  objects, cyclic structures, and dicts with non-`str` keys.
- **Every MCP parameter object has a `description` key (breaking for exact-key parsers, #65).**
  The change is additive, but a client that matches parameter keys exactly must now
  accept `description`.
- **`ontary validate --strict` exits 1 for undescribed actions and Functions (#65).**
  Default mode still exits 0 on warnings.

## [0.23.0] — 2026-10-07

```bash
uv add "ontary @ git+https://github.com/ryoochi0112/ontary@v0.23.0"
```

A minor release with breaking changes to list completeness. MCP `traverse_links`
now returns one page of at most 100 rows by default, so callers that read every row
must page with `limit` and `after`. MCP list results add `has_more`, and
`next_cursor` is `null` exactly when no more rows remain. In Python, `Page` and
`TypedPage` gain `has_more` as a required constructor field, so code that builds a
`Page` directly must pass it. The store schema stays at v14, so a 0.22.0 store needs
no re-ingest.

### Changed

- **MCP `traverse_links` returns a default page of 100 rows (breaking).** It used to
  return every row; pass `limit` (at most 1000) and `after` to page. (#63)
- **MCP `query_objects` and `traverse_links` add `has_more` (breaking for strict
  response parsers), and `next_cursor` is `null` exactly when `has_more` is `false`.**
  A page that ends exactly at the last visible row no longer returns a cursor. (#64)
- **Python `Page.next_cursor` is `None` on an exactly-full last page (breaking).**
  `Page` and `TypedPage` gain `has_more`. (#64)

### Added

- MCP `include_total` on `query_objects` and `traverse_links` adds a top-level `total`,
  the visible count (not min-N gated, same as `count_objects`). `limit=0` with
  `include_total=true` is a count-only mode. (#63)
- `client.traverse(..., limit=, after=)` returns a `Page`. (#63)

## [0.22.0] — 2026-10-07

```bash
uv add "ontary @ git+https://github.com/ryoochi0112/ontary@v0.22.0"
```

A minor release with one breaking change: reads refuse an unregistered object type
instead of returning an empty result. Callers that relied on `[]`, `0`, or `None` for
an unknown type must handle `UNKNOWN_OBJECT_TYPE`. Reads of declared types do not
change. The store schema stays at v14, so a 0.21.0 store needs no re-ingest.

### Changed

- **Reads refuse an unregistered object type (breaking for callers that relied on
  an empty result).** Python string-form reads (`list`, `count`, `get`, `exists`,
  `traverse`, `aggregate`, `aggregate_by`, `count_contributors`, the `GuardedQuery`
  methods, and `BoundQuery` reads in Functions) and the MCP read tools
  `query_objects`, `count_objects`, `get_object`, and `traverse_links` now raise
  `UNKNOWN_OBJECT_TYPE` for an undeclared type, where they used to return `[]`, `0`,
  or `None`. The type check runs before any other check and before any row is read.
  `traverse` with an unregistered anchor type now raises `UNKNOWN_OBJECT_TYPE`
  instead of `UNKNOWN_NAME`; an unknown link stays `UNKNOWN_NAME`
  ([#194](https://github.com/ryoochi0112/ontary/issues/194)).

## [0.21.0] — 2026-10-07

```bash
uv add "ontary @ git+https://github.com/ryoochi0112/ontary@v0.21.0"
```

A minor release with one breaking change to the MCP wire shape: read results now
say when the caller's view is limited by scope or redaction. Clients that check
exact keys on MCP read results must accept the new `scope_limited` and
`redacted_fields` keys. The Python surface is unchanged. The store schema stays at
v14, so a 0.20.3 store needs no re-ingest.

### Changed

- **MCP read results carry scope and redaction marks (breaking for clients that
  check exact keys).** `query_objects`, `traverse_links`, and `count_objects` add a
  top-level `scope_limited` boolean. Every row from `query_objects`, `traverse_links`,
  and `get_object` adds a `redacted_fields` list of the hidden property names. A
  redacted key stays absent from `payload`. `scope_limited` depends on declarations
  only and does not reveal how many rows were hidden. The Python surface is
  unchanged ([#62](https://github.com/ryoochi0112/ontary/issues/62)).

## [0.20.3] — 2026-10-06

```bash
uv add "ontary @ git+https://github.com/ryoochi0112/ontary@v0.20.3"
```

A docs-only patch release. The design guide's `ref()` scope note covers
`scope_ref()`, and English anchors resolve on the Japanese reference pages. No
code or API changes; the store schema stays at v14, so a 0.20.2 store needs no
re-ingest.

### Documentation

- **The design guide's `ref()` scope note covers `scope_ref()`.** Declaring the
  room with `scope_ref()` makes the engine check that the caller's scope covers
  it, but not that it shares the session's scope, so the handler check stays
  ([#185](https://github.com/ryoochi0112/ontary/issues/185)).
- **English anchors resolve on the Japanese reference pages.** The JA API
  reference hub and MCP page carry explicit anchors (`#mcp-server`,
  `#scope-policy`, `#stores`, `#descriptor-authoring`, `#error-codes`), so links
  from English-only pages land on the right section in the JA site. A test
  checks every such link
  ([#186](https://github.com/ryoochi0112/ontary/issues/186)).

## [0.20.2] — 2026-10-06

```bash
uv add "ontary @ git+https://github.com/ryoochi0112/ontary@v0.20.2"
```

A bugfix patch release for the newcomer path. The CLI imports a module from
the current directory, and a defaulted action parameter is optional on the
dynamic and MCP paths. The descriptor's `required` flag changes for such
parameters. No new public API; the store schema stays at v14, so a 0.20.1
store needs no re-ingest.

### Documentation

- **The design guide warns that `ref()` parameters are not scope-checked.**
  The `schedule_session` sketch (EN and JA) now says to check that a referenced
  room shares the session's scope once the types are scoped, and points to
  `examples/room_booking`
  ([#169](https://github.com/ryoochi0112/ontary/issues/169)).
- **The MCP reference lists each tool's required and optional arguments**, and
  says `execute_action` and `call_function` take one `params` object. A test
  checks the table against the live server's arguments, defaults and refusals
  ([#157](https://github.com/ryoochi0112/ontary/issues/157), F18).
- **Action and Function names, and ingest's type argument, are documented.**
  An action's `api_name` defaults to the params class name, and a Function's to
  the handler name (F13). `client.ingest` takes the type name as a string,
  while typed reads take the class (F12)
  ([#157](https://github.com/ryoochi0112/ontary/issues/157)).

### Fixed

- **`ontary validate` and `ontary serve` import a module from the current
  directory.** The console script now puts the working directory on
  `sys.path`, as `python -m` does, so `ontary validate app:ontology` works
  beside `app.py` without `PYTHONPATH=.`
  ([#120](https://github.com/ryoochi0112/ontary/issues/120)).
- **An action parameter with a Python default is optional.**
  `quantity: int = 0` was marked required, so omitting it on the dynamic
  (`execute(name, dict)`) and MCP paths was refused with `missing required
  parameter`. The handler now receives the default. The descriptor's
  `required` flag changes from `true` to `false` for such action and
  Function parameters; Functions already applied the default. `target()`
  and `scope_ref()` stay required even with a default, so the scope check
  always sees them
  ([#157](https://github.com/ryoochi0112/ontary/issues/157), F15).

## [0.20.1] — 2026-10-06

```bash
uv add "ontary @ git+https://github.com/ryoochi0112/ontary@v0.20.1"
```

A docs-only patch release. The API reference is split into a hub and seven
pages, and every anchor linked from elsewhere still resolves. No code or API
changes; the store schema stays at v14, so a 0.20.0 store needs no re-ingest.

### Changed

- **The API reference is split into seven pages under Reference**:
  `api-authoring`, `api-runtime`, `api-reading`, `api-actions-functions`,
  `api-stores`, `api-mcp` and `api-errors`, each with a `.ja.md` twin.
  `docs/api-reference.md` stays as the index and keeps every linked anchor.

## [0.20.0] — 2026-10-06

```bash
uv add "ontary @ git+https://github.com/ryoochi0112/ontary@v0.20.0"
```

This is the M2 release, "Examples and docs". Four reference apps now ship in
`examples/`: the reworked tickets desk, `maintenance_desk`, `room_booking` and
`bill_of_materials`, each with end-to-end tests and a README whose Python
blocks run under `make verify`. The docs site gains the Domain-Driven Design
page, `llms.txt` / `llms-full.txt`, an authenticated multi-consumer MCP
example, and in-process MCP testing notes. `MCPServer` is importable from the
package root and `BoundQuery.now()` reads the bound clock. **Dict-form Function
handlers, deprecated in 0.19.0, are removed**; `FunctionHandler` and the
`call_function` bad-argument error change with them. The store schema stays at
v14, so a 0.19.0 store needs no re-ingest.

### Added

- `examples/bill_of_materials/` is a fourth reference app: a parts master (Supplier, Part, BomLine) loaded with `OntologyClient.ingest` in object-then-link order under one `Source`, a reload that commits valid rows and reports bad ones, `explode_bom` / `where_used` / `parts_from_supplier` Functions built from single-hop `traverse` with a per-path cycle guard (cycles returned as data), no actions, every type unscoped, an MCP server, with end-to-end tests and a README whose Python blocks are executed by a test (#55).
- `examples/room_booking/` is a third reference app: room booking with an owned `Booking` relationship object that carries `starts_at`/`ends_at`, half-open time ranges queried with `where`, an overlap check in the booking action, reschedule as retire + create, one scope level and an MCP server, with end-to-end tests and a README whose Python blocks are executed by a test (#55).
- `examples/maintenance_desk/` is a second reference app: a work-order desk that shows transitions, rules, `Sensitivity`, scope, a snapshot type, `min_n`, events and an MCP server in one ontology, with end-to-end tests and a README whose Python blocks are executed by a test (#55).
- A new explanation page maps Domain-Driven Design terms to ontary, including what it does not support, and a docs test keeps that vocabulary off every other page (#53).
- The docs site now publishes `llms.txt`, a nav-ordered index with one-line page descriptions, and `llms-full.txt`, which puts every English page in one Markdown file with links made absolute. Both files are generated at build time from the nav (#59).
- `MCPServer` is importable from the package root (`from ontary import MCPServer`), so code can annotate what `build_mcp_server` returns (#61). It is the `mcp` SDK's class and needs the `[mcp]` extra. `import ontary` still works without the extra; touching `MCPServer` raises the builders' install hint, and in a core-only install so does `from ontary import *`. `__all__` now has 46 names.
- `docs/testing.md` (EN and JA) has a "Testing an MCP server in-process" section: drive the server through `httpx2.ASGITransport`, because a sync `TestClient` runs the app on another thread and the SQLite store then returns `INTERNAL_ERROR` for every tool call (#61).
- `docs/mcp-serving.md` has a complete authenticated multi-consumer example: a stand-in `TokenVerifier`, real `AuthSettings`, one `tools/call` request with its JSON response, and the `401` a missing or unknown token gets (#56). A doc test runs the program and replays the shown requests in-process, so the page fails `make verify` when it stops matching ontary or `mcp`.
- `BoundQuery.now()` gives a Function the current time from the runtime's bound clock, the same clock as `ctx.now()` (#154). The clock is read on first use and the instant is reused for the rest of the call. A time-dependent Function no longer needs a clock capability, which made every call audited.

### Removed

- Dict-form Function handlers on `@ontology.function` and `FunctionRegistry` (#103), deprecated in 0.19.0. Declare a `FunctionParams` subclass, or take only `(query)`; any other handler raises `ValidationFailed` `ONTOLOGY_INVALID` at declaration.
- `FunctionRegistry.register` no longer takes `mode` (#103).
- `FunctionDef.parameters` no longer accepts `None`, and MCP `list_functions` never publishes `parameters: null` (#103). A no-input function has `[]`.

### Changed

- Docstrings, comments (including SQL comment text in the store schema DDL; comments only), MCP tool descriptions, and the API reference no longer cite internal design documents, including ids such as `AC8`, `§5`, and `M10`; `ERROR_CODES` descriptions changed wording only (codes are unchanged), with no behavior changes. One declaration-time `ONTOLOGY_INVALID` message for restricted sensitivity that says "requires an Optional annotation" no longer ends in `(AC6)`, so code matching that exact string must update (#59).
- The tickets example drops the stored `Ticket.escalated` flag and derives it
  with `isTicketEscalated`. `OpenEscalation` is merged into `EscalateTicket`,
  and `ResolveTicket` / `ArchiveTicket` are renamed to `ResolveEscalation` /
  `ArchiveEscalation`.
- The design guide's "Links and object-backed link types" section (EN and JA) now defines relationships with facts as objects linked to participants. It shows a runnable Session, Room, and Assignment example that a doc test executes. The roadmap also records that links will not carry properties (#58).
- `client.call_function` raises `ValidationFailed` `INVALID_PARAMS` (was `TypeError`) for an argument that is neither a function name nor a `FunctionParams` instance (#103).
- The public `FunctionHandler` alias is now `Callable[..., Any]`, a `(query)` or `(query, params)` handler; it was `Callable[[BoundQuery, dict[str, Any]], Any]` (#103).
- `FunctionRegistry.function(api_name, params_cls=None)` takes an optional params class, matching `FunctionRegistry.register` (#103).
- The API reference states the `date` / `datetime` write format in one place, "Date and datetime values", and links it from `Store.insert` / `update`, `ActionContext.create` / `save`, bulk ingest, and action parameters (#57).

## [0.19.0] — 2026-10-04

```bash
uv add "ontary @ git+https://github.com/ryoochi0112/ontary@v0.19.0"
```

This release completes M1, "Model your operation". Actions can record events,
the business facts they produce, in their own transaction. Function parameters
are typed like action parameters. An injectable clock stamps every write, and
`ctx.now()` reads it. Lints now name the concrete fix and link to the design
guide, and `accept=` silences a lint at the declaration that caused it.
**The store schema moves to v14**, so a 0.18.0 store must be dropped and
re-ingested. Dict-form Function handlers are deprecated and are removed in
0.20.0.

### Added

- `EVENT_NEVER_EMITTED` lint (#47). Warns when no action declares an event in `emits`; the event's `accept=` can suppress it.
- Events (#47). `@ontology.event` declares a business fact as an `Event` subclass.
  An action lists the events it may emit with `emits=[...]`, and
  `ctx.emit(event, *, about=None)` records one inside the action's transaction.
  The subject defaults to the action's target. `client.events(event_type=None, /, *,
  about=None, since=None, until=None)` reads them as `EventRecord`, governed by the
  subject's latest scope and `row_visibility`, with `Sensitivity` redaction. An `ok`
  `AuditEntry` lists its emitted events in `AuditEntry.events`, and
  `Scenario.then_event` checks them in tests. `Event` and `EventRecord` join the
  root exports.
- `ActionTypeDef.emits` (#47). It lists the api names of the events an action may
  emit. MCP `list_action_types` shows them as `emits`.
- Event error codes (#47). `UNDECLARED_EVENT` reports an action emitting an
  event type outside its `emits` declaration; `EVENT_SUBJECT_INVALID` reports
  an event whose subject cannot be resolved to a valid target object.
- Typed Function parameters (#45). A `FunctionParams` subclass validates a
  function's inputs, types the handler, and exposes its parameter definitions
  through MCP `list_functions`. A `(query)` handler declares a function with no
  inputs, and `call_function(api_name)` no longer needs a params argument.
- `ctx.now()` and a store-level clock (#46). `ontology.bind(store, clock=...)`
  installs the clock on the store, and it stamps every write: actions,
  `bulk_upsert` / `bulk_link`, and direct store calls. One action invocation
  reads the clock once, and that instant is used for `ctx.now()`, every
  `valid_from` / `valid_to` it writes, and every audit `ts`.
- Clock error codes (#46). `CLOCK_CONFLICT` (binding a store with a second
  clock), `CLOCK_REGRESSION` (a write earlier than the `valid_from` it closes),
  and `CLOCK_NOT_TIMEZONE_AWARE` (a clock returned a naive `datetime`).
- Two new advisory lints (#50). `FREE_TEXT_STATUS` fires on a `status` or `*_status`
  property typed `str` with no declared choices. `AUDIT_TYPE` fires on an object
  type name ending in `AuditLog`, `AuditEntry`, `AuditTrail`, `AuditRecord`, or
  `AuditEvent`.
- `Finding.guide` (#50). It holds the URL of the design-guide section that explains
  the finding, or `None`. All nine advisory codes set it. `ontary.diagnose` exports
  `GUIDE_URL`, `GUIDE_ANCHORS`, and `ADVISORY_CODES`, and `ontary validate` prints a
  `guide:` line under `fix:` and a `guide` key in `--json`.
- `accept=` on `prop`, `@ontology.object`, `@ontology.action`, and
  `@ontology.function` (#50). It accepts a modelling lint at the declaration that
  caused it. A code the declaration cannot accept is refused with
  `ONTOLOGY_INVALID`.
- `snapshot=True` on `@ontology.object` (#50). It declares a snapshot type, which
  gets no `STORED_DERIVABLE` finding and no `FORBIDDEN_TYPE_NAME` finding for a
  `Snapshot` suffix.
- `ontary validate --strict` (#50). It also exits 1 when any `warn` finding remains.
  `info` findings never change the exit code.

### Changed

- **Breaking (store schema):** `SCHEMA_VERSION` is now 14. `audit_log` has a new
  `events` column. A store stamped 13 is refused with `STORE_VERSION_UNSUPPORTED`;
  drop and re-ingest it as described in
  [docs/storage.md](docs/storage.md#moving-across-a-schema-version).
- Lint messages and fix hints changed (#50). They now name the concrete fix, using
  your own declaration names. The codes and severities did not change.
- `CRUD_ACTION_NAME` now covers Functions as well as actions (#50). It compares the
  first word of the api name, ignoring letter case, so `SettleInvoice` no longer
  warns.
- `FORBIDDEN_TYPE_NAME` now catches a year suffix such as `Survey2024` (#50). A type
  named exactly `History` or `Snapshot` no longer fires, because it is not a clone
  of another type.
- The text "declared snapshot" in an object's `description` no longer exempts the
  type from `STORED_DERIVABLE` or `FORBIDDEN_TYPE_NAME` (#50). Use `snapshot=True`.
- In the tickets example, `Ticket.status` is now a choice property, so
  `ontary validate --strict` passes on it (#50).
- Audit `ts` is now the instant the invocation started (#46). It was read after
  the handler finished. Bind the clock before seeding data, or a clock set in
  the past can raise `CLOCK_REGRESSION` on the first update.

### Deprecated

- Dict-form Function handlers (#45). They remain supported in 0.19.0 with a
  declaration-time `DeprecationWarning` and are removed in 0.20.0; use a
  `FunctionParams` subclass.

### Fixed

- `prop(choices=[...])` on an `ActionParams` field now sets `choices` on its
  `ActionParameterDef`, as it does on a property (#90). Before, it was silently
  ignored, so the parameter accepted any string. Combining it with an `Enum` or
  `Literal` annotation, or declaring invalid choices, is refused as
  `ONTOLOGY_INVALID`, as on a property.
- A `(str, Enum)` mixin member used as a primary key or as an id argument now
  keys the object by its string value (`'a'`) on every store (#91). Before, the
  in-memory and Postgres stores keyed it by `str()` (`'Mixin.A'`), `insert`
  returned `'Mixin.A'` on all three, and re-ingesting the row with `bulk_upsert`
  on SQLite raised a raw `IntegrityError`. Every `Store` method and
  `bulk_upsert`/`bulk_link` now reduce a `str` subclass to its plain value.
  Postgres rows already stored under a `'Mixin.A'`-style id are not rewritten.

## [0.18.0] — 2026-09-28

```bash
uv add "ontary @ git+https://github.com/ryoochi0112/ontary@v0.18.0"
```

This release lets a model say what its values may be and how they may change.
Choice properties (`Enum` and `Literal`) and struct properties are typed on read
and checked on write. Declared status transitions and named object rules are
enforced on every write path. The string `ActionContext` members deprecated in
0.17.0 are removed; use the typed members. The store schema stays at v13, so a
0.17.0 store needs no re-ingest.

### Added

- Choice properties (#42). A property or action parameter annotated with a
  string-valued `Enum` or a `Literal[...]` of strings declares `type="str"` with
  its member values as `choices`. The store keeps the string value, every write
  path accepts the member or its value, and a typed read returns the member, so
  `mypy` narrows it. `ActionParameterDef` gains `choices`, enforced as
  `INVALID_PARAMS`, and MCP's `list_object_types` and `list_action_types` now list
  `choices` for properties and parameters. A non-string member, a choice
  annotation combined with `prop(choices=...)`, and a choice annotation on the
  primary key are refused as `ONTOLOGY_INVALID`.
  An `Enum` member is unwrapped to its value by the declaration, in one place
  (`typesys.choice_value`), before validation and before the object id is read:
  only a property or parameter that declares `choices` accepts it. The one
  deliberate behaviour change for an existing declaration is that a
  `prop(choices=...)` property (including a `choices`-declared primary key) now
  accepts an `Enum` member whose value is one of its choices, since it is the
  same declaration. A `str` property without `choices` refuses an `Enum` member
  exactly as before.
- Struct properties and action parameters (#43). A flat Pydantic model
  annotation declares `type="struct"` with its inner fields exposed through
  `PropertyDef.fields` or `ActionParameterDef.fields`. `StructFieldDef` describes
  each inner field. MCP schema discovery includes `fields` (null for non-structs).
  An absent optional inner field is stored as explicit `null`, so a dict write
  hydrates like the equivalent model instance. Struct inner fields may only
  default to `None`. This additively widens
  `PropertyType`; `SCHEMA_VERSION` is unchanged.
- Declared status transitions and named object rules (#44). Choice properties
  can carry a `TransitionDef` graph, and `Ontology.rule` registers a typed
  predicate over the full object. The write paths enforce allowed state moves
  and rule outcomes with `TRANSITION_NOT_ALLOWED` and `RULE_VIOLATED`. These
  declarations do not change `SCHEMA_VERSION`.

### Changed

- Every store update now reads the current row inside its transaction before
  applying an update. MCP `list_object_types` adds a `transitions` graph for
  each governed property and a `rules` list for each type. `RuleDef` carries a
  Python callable, excluded from `model_dump()`.
- `list[<BaseModel>]` and `dict[..., <BaseModel>]` annotations now raise
  `ONTOLOGY_INVALID` with the fix "use a linked object type". Previously they
  derived `type="json"`; `prop(property_type="json")` does not bypass the refusal.
- A model annotation with an explicit `prop(property_type=...)` other than
  `"json"` now raises `ONTOLOGY_INVALID`. `property_type="json"` still stores
  the model opaque.
- A `RootModel` annotation now raises `ONTOLOGY_INVALID`, including with an
  explicit `property_type="json"`. An unadorned `RootModel` was already refused
  before #43; a property with an explicit JSON override previously accepted it.

### Removed

- Removed `ActionContext.insert(obj_type, payload)`; use `create(cls, **values)`.
- Removed `ActionContext.update(obj_type, obj_id, changes)`; use
  `get(cls, obj_id)`, assign the changed properties, and call `save(obj)`.
- Removed `ActionContext.create_link(link_api_name, from_id, to_id)`; use
  `link(handle, from_, to)`.
- Removed `ActionContext.read_current(obj_type, obj_id)`; use `get(cls, obj_id)`.
- Removed `ActionContext.read_all(obj_type)`; use `all(cls)`.
- Removed `ActionContext.links_from(link_api_name, from_id)`; use
  `traverse(handle, anchor)`.
- Removed `ActionContext.links_to(link_api_name, to_id)`; use
  `traverse(handle, anchor, reverse=True)`.
- Passing a `str` as the first argument to `retire` or `unlink` raises
  `ValidationFailed` with code `INVALID_PARAMS`. Use `retire(obj)` or
  `retire(cls, obj_id)`, and `unlink(handle, from_, to)`.
- `ActionContext.unlink` is positional-only, so a keyword call raises
  `TypeError`; the typed overload was already positional-only.
  See [ontary#41](https://github.com/ryoochi0112/ontary/issues/41).

## [0.17.0] — 2026-09-27

```bash
uv add "ontary @ git+https://github.com/ryoochi0112/ontary@v0.17.0"
```

This release makes action handlers typed. A handler reads and writes through the
ontology's own classes and link handles, and `mypy` checks every field it touches.
The string `ActionContext` members still work but warn, and they are removed in
0.18.0. Every non-`ok` audit entry now records its target and error code. The store
schema moves to v13, so a v12 store must be re-ingested.

### Added

- A typed `ActionContext` surface. `ctx.get(Order, id) -> Order | None`,
  `ctx.all(Order)`, `ctx.create(Order, **values) -> Order`, and `ctx.save(order)`
  read and write through the ontology's own classes. `save` writes only the declared
  properties changed since the context handed the object out. `ctx.link`,
  `ctx.unlink` and `ctx.traverse` take a `LinkHandle` with each end as an object or
  its id, and `ctx.retire(order)` or `ctx.retire(Order, id)` retires. `mypy`
  checks the class, the returned type, each link endpoint's type, and every
  attribute assigned before `save`. The new code `OBJECT_NOT_LOADED` (kind
  `validation`) refuses `save` on an object this context did not hand out.

### Deprecated

- The string `ActionContext` members now emit a `DeprecationWarning` that names the
  typed replacement: `insert` → `create`, `update` → `get` + `save`, `create_link`
  → `link`, `read_current` → `get`, `read_all` → `all`, and `links_from` /
  `links_to` → `traverse`. The string forms of `retire` and `unlink` warn as well;
  their typed forms keep the same names. `retire`'s own link cascade does not warn.
  The string members are removed in 0.18.0
  ([ontary#41](https://github.com/ryoochi0112/ontary/issues/41)). The README, the
  docs, and `examples/tickets` now use the typed members.

### Changed

- **Breaking (store schema):** `SCHEMA_VERSION` is now 13. `audit_log` has a new
  `error_code` column. A store stamped 12 is refused with
  `STORE_VERSION_UNSUPPORTED`; drop and re-ingest it as described in
  [docs/storage.md](docs/storage.md#moving-across-a-schema-version).

### Fixed

- Every non-`ok` audit entry now says what failed and why. An action entry records
  the requested `target_id` on every outcome; before, only the `ok` entry had it,
  and `denied` and `error` entries wrote `None`. The new `AuditEntry.error_code`
  records the raised exception's code on every `denied` and `error` entry, for
  actions and functions alike. An exception without a code is recorded as
  `INTERNAL_ERROR`, as the MCP server reports it. Fixes
  [ontary#49](https://github.com/ryoochi0112/ontary/issues/49).

## [0.16.0] — 2026-09-27

```bash
uv add "ontary @ git+https://github.com/ryoochi0112/ontary@v0.16.0"
```

This release completes M0, "Trust the core". A link needs a live object at both
ends, and re-creating a link is a no-op. An action may target an unscoped type.
`datetime` objects are accepted on write, and authoring mistakes raise catalogued
errors. `diagnose()` catches a `target()` mismatch and stored rows that an edited
ontology can no longer read. The store schema moves to v12, so a v11 store must be
re-ingested.

### Changed

- **Breaking (store schema):** `SCHEMA_VERSION` is now 12. `audit_log` has a new
  `unscoped_params` column. A store stamped 11 is refused with
  `STORE_VERSION_UNSUPPORTED`; drop and re-ingest it as described in
  [docs/storage.md](docs/storage.md#moving-across-a-schema-version).
- An action may now target a `scope="unscoped"` type. A `target(...)` parameter
  that refers to an unscoped type skips the action scope gate, so the action's
  `roles=` is its only gate. Before, every scope level resolved to nothing and
  every consumer was denied with `SCOPE_DENIED`, so reference data could never be
  an action's target. The new `AuditEntry.unscoped_params` names the parameters
  that skipped the gate. A `scope_ref(...)` parameter may not refer to an
  unscoped type; `validate()` and `diagnose()` report it as `SCOPE_POLICY_ERROR`.
  Fixes [ontary#35](https://github.com/ryoochi0112/ontary/issues/35).
- **Breaking (behaviour):** a link now needs a live object at both ends.
  `Store.create_link`, and therefore `ActionContext.create_link` and
  `client.ingest_links`, refuse an endpoint id that is missing, retired, or live
  only as another type with the new code `LINK_ENDPOINT_NOT_FOUND` (kind
  `validation`), checked before cardinality. `ingest_links` reports it per pair
  and still commits the other pairs. Before, such a link was stored dangling.
  Ingest objects before their links. Fixes
  [ontary#36](https://github.com/ryoochi0112/ontary/issues/36).
- `create_link` is now idempotent for every cardinality: a link identical to a
  live one is a no-op (nothing written, no `WriteRecord`), checked before
  cardinality. Before, a MANY_TO_MANY re-create inserted a duplicate row and a
  MANY_TO_ONE re-create raised `CARDINALITY_VIOLATION` against itself; re-running
  `ingest_links` is now idempotent and returns the same report. The store schema
  gains a partial unique index `idx_links_live_pair` on live links as a storage
  backstop; `SCHEMA_VERSION` stays 12, which this release introduces, so a store
  created from an unreleased 12 lacks the index but is still guarded by the
  engine check. Fixes [ontary#37](https://github.com/ryoochi0112/ontary/issues/37).

### Fixed

- `validate()` and `diagnose()` now catch two modelling mistakes that used to pass
  and fail later. An action whose `target(...)` parameters all refer to another
  type than its `target=` is refused at `@ontology.action(...)` with
  `ONTOLOGY_INVALID`; before, the scope gate checked the parameter's object while
  audit and MCP named `target=`. `OntologyRegistry.validate()` and `diagnose()`
  apply the same rule to hand-built registries, so a hand-built action with that
  shape that validated before is now refused. `Ontology.diagnose(store=...)` and
  `Ontology.validate(store=...)` sweep a store's current rows and report, per type
  and property, the rows that would fail hydration under an edited ontology
  (`INVALID_RECORD`); before, the store opened cleanly and the first read failed.
  Fixes [ontary#40](https://github.com/ryoochi0112/ontary/issues/40).
- Authoring mistakes are refused where they are written, with
  `ValidationFailed` (`ONTOLOGY_INVALID`) naming the class or link, the kwarg, what
  was given, and the accepted forms. Before, a misspelled `cardinality` escaped as a
  bare `ValueError` from `ontology.link(...)`, and a misspelled `scope="unscoped"`,
  a rule not wrapped in a list, or a non-callable `row_visibility` surfaced only at
  `validate()` as a raw pydantic error from inside `ScopePolicy`. An empty or
  duplicated `scope_levels` and a `min_n` below 1 are wrapped the same way when
  `.definition` is built. `cardinality` and `scope` are now `Literal`-typed, so
  the typo is a mypy error as well; the runtime still accepts any string that
  names a member. Fixes [ontary#39](https://github.com/ryoochi0112/ontary/issues/39).
- An offset-aware `datetime` object is now accepted wherever a `datetime` property
  or parameter takes a value: `Store.insert`/`update` (and so `ActionContext`),
  `bulk_upsert`/`client.ingest`, `where` operands, and action parameters in both
  the typed and the dict form. It is persisted as its own `isoformat()` spelling,
  offset preserved, so `eq` keeps matching what was written and the typed read
  hydrates it back to an equal `datetime`. A naive `datetime` object is refused
  with a message that names the problem (`INVALID_RECORD`, `INVALID_PARAMS`, or
  `OPERATOR_TYPE_MISMATCH` by path). Before, every `datetime` object was refused
  with "expected type 'datetime', got datetime", and a naive one slipped through
  the typed action form as a naive string. ISO strings behave exactly as before.
  A `date` object in a dict-form action parameter is likewise converted instead of
  failing the JSON round-trip check. Fixes
  [ontary#38](https://github.com/ryoochi0112/ontary/issues/38).
- `docs/api-reference` listed `PropertyType` without `"date"`; the literal now
  matches the code.

## [0.15.0] — 2026-09-27

```bash
uv add "ontary @ git+https://github.com/ryoochi0112/ontary@v0.15.0"
```

A primary key now identifies exactly one live object. `insert` refuses a live
duplicate and `update` refuses a primary-key change, each with a new catalogued
code. This bumps the store schema to v11, so a v10 store must be re-ingested.

### Changed

- **Breaking (store schema):** `SCHEMA_VERSION` is now 11. The new
  `idx_objects_live_id` partial unique index allows at most one live row per
  `(tenant, object_type, id)` in SQLite and Postgres. A store stamped 10 is refused
  with `STORE_VERSION_UNSUPPORTED`; drop and re-ingest it as described in
  [docs/storage.md](docs/storage.md#moving-across-a-schema-version).

### Fixed

- `insert` (and `ctx.insert` inside an action) now refuses a primary key that
  already has a live row of the same object type, with `ConflictError` and the
  new catalogued code `OBJECT_ALREADY_EXISTS`. Before, every store accepted the
  duplicate: `read_all` returned both rows while `read_current` kept returning the
  old one. A retired object's id may still be inserted again. Fixes
  [ontary#34](https://github.com/ryoochi0112/ontary/issues/34).
- `update` (and `ctx.update` inside an action) now refuses a change that gives the
  primary key a different value, with `ValidationFailed` and the new catalogued
  code `PRIMARY_KEY_IMMUTABLE`. Before, every store accepted it: the payload
  primary key changed while the store id kept its old value, so two live objects
  could carry the same primary key and `read_current(type, "b")` could return a
  row whose payload said `id == "a"`. Repeating the current value is still
  accepted. To re-key an entity, retire it and insert a new object. Fixes
  [ontary#75](https://github.com/ryoochi0112/ontary/issues/75).
- An ungrouped `aggregate` that misses min-N with a supplied `value_field` no
  longer reports `<type>.<field> group None`; only a real `group_by` names a
  group in the `MIN_N_VIOLATION` message. Review backlog from
  [ontary#27](https://github.com/ryoochi0112/ontary/issues/27).

## [0.14.0] — 2026-09-14

```bash
uv add "ontary @ git+https://github.com/ryoochi0112/ontary@v0.14.0"
```

One ergonomics change: `aggregate(func="count")` no longer needs a `value_field`
and accepts any declared field type, so it is the released, min-N-gated count.
`count_objects` is unchanged but now documented as not min-N-gated.

### Changed

- `aggregate` / `aggregate_by` / MCP `aggregate_objects`: `func="count"` now
  accepts any declared field type (no numeric coercion) and may omit
  `value_field` to count every visible row under min-N; every other func with
  no `value_field` refuses with `INVALID_PARAMS`. `NON_NUMERIC_AGGREGATE` now
  names the requested function. Resolves
  [ontary#25](https://github.com/ryoochi0112/ontary/issues/25).

### Documentation

- `count_objects` (MCP tool description, `docs/api-reference.md`/`.ja.md`,
  `docs/mcp-serving.md`) now states it is a visible-row count that is not
  min-N-gated, and points to `aggregate_objects(func="count")` as the
  released alternative.

## [0.13.0] — 2026-09-13

```bash
uv add "ontary @ git+https://github.com/ryoochi0112/ontary@v0.13.0"
```

Three new docs pages (getting-started, CLI reference, testing; EN and JA) and one
tightening: a declared `datetime` must carry a time component, so date-only
strings that 0.12.0 accepted on write are now refused.

### Documentation

- Three new docs pages, English and Japanese: a getting-started tutorial
  (`docs/getting-started.md`), the CLI reference (`docs/cli.md`: `ontary validate`,
  `ontary serve --dev`, `ontary version`, `ontary-mcp`, exit codes), and testing an
  ontology with `ontary.testing` (`docs/testing.md`). The tutorial's whole program
  and the testing page's test functions are executed by `tests/test_docs.py`.
- README gains a "Command line" section; `docs/mcp-serving.md` shows the
  `ontary serve --dev` one-liner next to the Python builder.
- `docs/storage.md` states which role applies the PostgreSQL RLS policies: the one
  that constructs the store on an empty database, which needs `CREATE` on the schema
  and becomes the table owner. Roles that connect later need only table DML.

### Changed

- A declared `datetime` value must carry a time component. A date-only string
  such as `"2026-02-15"` parsed as a naive midnight, so it was accepted on write
  and, as a `where` comparison operand, silently never matched an offset-aware
  column. It is now `INVALID_RECORD` on write and `OPERATOR_TYPE_MISMATCH` as an
  operand. Naive and offset-aware datetimes are both still accepted.

## [0.12.0] — 2026-09-08

```bash
uv add "ontary @ git+https://github.com/ryoochi0112/ontary@v0.12.0"
```

mcp 2.x, hardened CI, and the first two SDK gaps picked from real use: ranges
in `where` and `datetime` comparisons. The mcp bump is breaking; pin
`ontary<0.12` to stay on mcp 1.x.

### Breaking

- `ontary[mcp]` now requires `mcp>=2.1.1,<3` (was `>=1.27.2,<2`); mcp 1.x is
  no longer supported — on 1.x the builders raise `ImportError` naming the range
  to install.
- Both `build_mcp_server` and `build_multi_consumer_mcp_server` return
  `mcp.server.mcpserver.MCPServer` (was `mcp.server.fastmcp.FastMCP`).
- `build_multi_consumer_mcp_server` no longer forces `stateless_http=True`; pass
  transport options (`stateless_http`, `json_response`, `transport_security`, `host`;
  `port` on `run()`) to `run()`/`streamable_http_app()`. On mcp 2.x each request
  resolves its own token in stateful sessions too.
- The twelve tool handlers are `async def` (2.x runs sync handlers on a worker
  thread, which the thread-affine SQLite store cannot serve); a `ConsumerResolver`
  stays a synchronous callable and now runs on the event loop.
- To stay on mcp 1.x, pin `ontary<0.12`.

### Added

- `where` conditions accept several operators on one field, AND-ed:
  `{"decided_at": {"gte": a, "lt": b}}` is a range. Before, a mapping with more
  than one key was refused as `UNKNOWN_OPERATOR`, so a half-open range over one
  property could not be written. A multi-key mapping stays learning-shaped for
  the hidden scope-key exemption, even when it contains an `in`.
- `gt`/`gte`/`lt`/`lte` on declared `datetime` properties, compared as instants
  across UTC offsets. Before, comparisons were limited to `int`, `float`, and
  `date`, so an ISO datetime column could only be matched exactly.
- `docs/storage.md` documents the drop-and-recreate path across a schema stamp
  (9 under `ontos` → 10 under `ontary`), since neither backend has a migration
  ladder.
- CI tests on Python 3.13 as well as 3.12, matching the classifiers.
- `codeql.yml`: CodeQL for Python on every PR, on `main`, and weekly.
- A `workflows` job in `verify.yml` lints the workflows with zizmor, so an
  unpinned action or a checkout that keeps its credentials fails the PR.
- `SECURITY.md`: supported versions, private vulnerability reporting, and what
  counts as a vulnerability in the engine.
- Dependabot (`.github/dependabot.yml`) for `uv` and GitHub Actions, weekly,
  grouped, with a 7-day cooldown, never automerged.

### Changed

- Every workflow action is pinned to a commit SHA; every workflow declares
  `permissions: contents: read` at the top, a timeout on every job, and
  `persist-credentials: false` on checkout. `verify` cancels a superseded run
  on a PR branch only.
- The release gate no longer uses the uv cache: the job builds the bytes that
  are published, and a cache shared with PR runs is a poisoning surface.
- `renovate.json` removed. The Renovate app had never been installed on the
  repository, so the config was inert; Dependabot replaces it.
- The ruff rule set is declared explicitly (`select = ["E4", "E7", "E9", "F", "I",
  "B", "C901"]`) instead of extending the implicit default. ruff 0.16.0 widened
  the defaults from 59 to 413 rules; the explicit list keeps the pre-0.16 gate.
- Dependabot's `mcp` major-bump ignore was added for PR #10 and removed again by
  this change now that the migration has landed.

## [0.11.0] — 2026-09-06

```bash
uv add "ontary @ git+https://github.com/ryoochi0112/ontary@v0.11.0"
```

OSS v0: `ontary` is cut to its core and published with a docs site. Every
removed public name is listed under `### Removed`; this is the first release
that ships without them. Pre-v1 hygiene from the same cycle is under
`### Changed`.

### Added

- Docs site at https://ryoochi0112.github.io/ontary/ (English / 日本語): MkDocs
  Material + `mkdocs-static-i18n`, built strict on every PR and deployed from
  `main` by `.github/workflows/docs.yml`. `make docs` serves a local preview.
- The stranger test in CI: the wheel is installed with `[mcp]` into an empty
  venv, then the README quickstart and the tickets example run from it
  (`scripts/stranger_smoke.py`).
- `docs/releasing.md` is back as a minimal runbook for the tag-driven PyPI publish; it replaces the v1-gate version listed under Removed.
- The README quickstart now declares two object types and one action, opens a
  SQLite store, and builds an MCP server.

### Removed

- The v1 acceptance gate, upgrade-fixture ladder, and `upgrade-fixture-honesty` CI job (docs/v1-gate.md, the v1-gate docs/releasing.md, tests/fixtures/upgrade).
- The storage envelope: `STORAGE_ENVELOPE_EXCEEDED` finding, `Ontology.diagnose(store=)`, `ontary validate --store`, scripts/scan_curve.py.
- Error code `STORE_SCHEMA_INCOMPATIBLE`.
- ontary.explain (DecisionTrace, explain_read, explain_list, explain_scan) and the `ontary explain` CLI.
- ontary.erase, Store.erase_object_content, Store.object_erasure_state, EraseResult, the `ontary erase` CLI, and codes OBJECT_ERASURE_NOT_FOUND, OBJECT_ALREADY_ERASED.
- Effects and the durable outbox: EffectDispatcher, EffectHandle, EffectMeta, EffectPayload, OutboxRecord, RetryPolicy, DrainReport, Ontology.effect, Ontology.action(effects=), ActionContext.emit, OntologyRuntime.drain_effects, OntologyClient.drain_effects, OntologyClient.outbox, ontary.testing.capture_effects, AuditEntry.effects, the effect_outbox table, and codes EFFECT_NOT_DISPATCHABLE, UNDECLARED_EFFECT, EFFECT_NOT_SERIALIZABLE.
- Ontology fingerprints and drift detection (ontary.fingerprint, Store.read/write_ontology_fingerprint, accept_ontology_drift=, code ONTOLOGY_DRIFT); declared type versions and upcasters (Ontology.object(version=), Ontology.upcaster, ontary.upcast, code UPCAST_FAILED); ontary.migrate (migrate_object_type, upcast_object_type).
- Connectors: ontary.connect (BaseConnector, CanonicalBatch, CanonicalRecord, MappingSpec, ObjectBinding, LinkBinding, RawTables, oid, run_pipeline), the dlt and bq extras, docs/connectors.md, and codes ENTITY_KEY_MISMATCH, MISSING_MAPPED_FIELD. Bulk loading stays via OntologyClient.ingest / ingest_links.
- docs/authority.md, docs/queries.md, docs/cookbook.md (content folded into docs/api-reference.md and examples/tickets/README.md); the Compatibility project URL.

### Changed

- docs/compatibility.md is now a one-paragraph pre-1.0 policy; CHANGELOG's Removed/Changed sections are the migration guide.
- `SCHEMA_VERSION` is bumped 9 → 10, and stores are now create-or-refuse: both the
  SQLite and the Postgres backend refuse a store stamped at any other version with
  `STORE_VERSION_UNSUPPORTED`, and neither migrates in place. A store created by
  0.10.0 or earlier (stamped 9) must be recreated and reloaded from its source —
  for example via `OntologyClient.ingest`. The internal mixin
  `ontary.store.migration.SqliteSchemaMigrator` is renamed `SqliteSchemaGate` to
  match: it gates, it does not migrate.
- Decision B: exact-scope-id match is the v1 contract; parent-covers-child
  coverage is opt-in and post-1.0. The `covers_scope` and `ScopePolicy`
  docstrings cite the decision instead of calling it deferred, and a contract
  test pins that a parent-scoped consumer never covers a child-owned row.
- Every GitHub Actions `uses:` entry in `verify.yml` and `release.yml` moved off the
  Node 20 runtime: `checkout` v7, `setup-uv` v10.0.1, `upload-artifact` v7,
  `download-artifact` v8.
- `release.yml` serializes runs per tag (`concurrency`, `cancel-in-progress: false`)
  so a re-push cannot cancel a publish that is already uploading.

## [0.10.0] — 2026-09-04

```bash
uv add "ontary @ git+https://github.com/ryoochi0112/ontary@v0.10.0"
```

### Changed

- No change to the published storage envelope this crossing: a governed `aggregate`
  or narrow-scope read still stays under one second below ~32,000 rows on `ObjectStore`
  (SQLite) and ~1,600 rows on `PostgresStore` (PostgreSQL) — the same figures `[0.9.0]`
  published below.
- An action handler's `ctx.insert(obj_type, payload)` now fills a missing primary
  key from the runtime's configured `id_factory` before calling `store.insert`,
  instead of falling through to the store's own `uuid.uuid4()` — the same seam
  invocation and effect ids already come from. A direct `store.insert` call
  (bypassing an `ActionContext`) is unaffected and keeps minting `uuid.uuid4()`.
  See [docs/compatibility.md § Auto-minted ids come from `id_factory` (0.9 →
  0.10)](docs/compatibility.md#auto-minted-ids-come-from-id_factory-09--010).
- `docs/storage.md#storage-envelope` now states its publication rule outright and
  adds an observed-run log, closing a silent SELECTION the doc never disclosed: a CI
  run of the Postgres storage-envelope curve step earns a `#### Run <id>` table only
  when it sets or lowers the published floor, and every run actually read — not only
  the ones that earn a table — is listed by id and its own worst per-row rate
  regardless. Two runs (`33704034596`, `33705109711`) had been read and silently
  dropped from the doc, with nothing anywhere in the tree recording that they ever
  ran; both are now visible in the observed log, and neither moves
  `STORAGE_ENVELOPE["PostgresStore"]` — 1,600 rows at 0.000609s/row, unchanged.
  `OBSERVED_POSTGRES_RUN_IDS` (`tests/test_docs.py`) pins this log the same
  structural way the existing `PUBLISHED_POSTGRES_RUN_IDS` already pins the four run
  tables, and is required to be a superset of it.
- The memo-speedup table under `docs/storage.md#storage-envelope` — `2.45x`/`2.58x`
  on `ObjectStore`, `3.02x`/`3.10x` on `PostgresStore` — was an unattributed pair of
  percentages; it now names its source. The `PostgresStore` pair is run 1's
  (`33623316425`) own n=25,000 measurement, already published in full above it in
  the same section, not a separate benchmark run; the `ObjectStore` pair is labelled
  for what it always was, a single local laptop sample, never CI-produced. Neither
  number changed — this is a provenance correction, not a re-measurement. (The
  `## [0.9.0]` entry below quoting "2.45x-3.10x faster ... on both backends" is
  unaffected: those are the same two figures, and they check out against this now
  fully-attributed source.)
- The `## [0.9.0]` entry below quotes "cutting the store calls a governed
  `aggregate` or narrow-scope read issues from 7 to ~2 per row" as one combined
  figure, with no measurement basis stated for either read class next to it.
  `docs/storage.md#storage-envelope`'s own supporting detail only shows the
  `aggregate` side (profiling `aggregate()` against `PostgresStore` at 8,000 rows:
  16,006 calls, ~2 per row, down from 7) and never states a narrow-scope figure at
  all, so the `[0.9.0]` sentence silently generalized from one measured class to
  both without saying so. Both classes were in fact counted directly, on
  `ObjectStore` (SQLite) at 2,000 rows: `aggregate` 7.00 → 2.00 store calls per
  row, narrow-scope list 7.00 → 2.00 store calls per row — a single local count,
  never CI-produced, the same evidentiary class as this project's other
  `ObjectStore`/SQLite figures. The `[0.9.0]` claim itself is unchanged and was not
  wrong, only unattributed for narrow-scope; this is a provenance correction, not a
  re-measurement.

## [0.9.0] — 2026-09-03

The storage-envelope release: decision C (how large may one object type get?) is
resolved with a measured, published number and an advisory runtime signal, never an
enforced ceiling. No path that succeeded at 0.8.0 fails here, and every existing read
answers exactly what it answered before — most of them faster.

```bash
uv add "ontary @ git+https://github.com/ryoochi0112/ontary@v0.9.0"
```

### Added

- [`docs/storage.md#storage-envelope`](docs/storage.md#storage-envelope) publishes a
  measured, reproducible envelope for both shipped backends: a governed `aggregate` or
  narrow-scope read stays under one second below ~32,000 rows on `ObjectStore` (SQLite)
  and ~1,600 rows on `PostgresStore` (PostgreSQL — a conservative floor measured across
  four CI `postgres:16` runs: a GitHub-hosted runner is a shared VM, not fixed hardware,
  so the doc publishes all four runs' dispersion rather than a single point estimate). A
  bounded, unordered page read is flat at either size and carries no
  measured ceiling. The Postgres curve is loopback-measured; the doc publishes the
  correction arithmetic (roughly `+ 2 x RTT x rows` post-memo, down from `+ 7 x RTT x
  rows` pre-memo) an operator applies for their own network's round-trip time.
- `ontology.diagnose(store=...)` gains one advisory rule: a `Finding` (code
  `STORAGE_ENVELOPE_EXCEEDED`, `severity="warn"`) fires per object type whose stored
  row count, counted per tenant and never as a cross-tenant total, crosses its
  backend's published envelope. Passing no `store`, or a store under the envelope,
  returns no such finding. Nothing refuses, and this adds no error code.
- `OntologyRuntime.explain_scan(consumer, obj_type, where=None)` returns a `ScanReport`
  (`rows_scanned`, `rows_returned`, `rows_hidden_by_scope`) for one governed read. It
  follows `DecisionTrace`: reachable only from `OntologyRuntime`, absent from
  consumer-bound clients, `OntologyClient`, and MCP.
- A call-scoped memo (`ontary.scope._ScopeReadCache`) now reads a shared parent row at
  most once per guarded read instead of once per scanned row, cutting the store calls a
  governed `aggregate` or narrow-scope read issues from 7 to ~2 per row. Measured
  2.45x-3.10x faster at 25,000 rows across both linear read classes on both backends —
  comfortably past the >= 2x this decision required. It is proven result-identical,
  including where a `ScopePolicy.row_visibility` predicate or a `CustomResolver`
  participates in the visibility decision, and its read-once-per-guarded-read behavior
  is documented as an implementation detail of the current engine, not an API guarantee
  ([docs/storage.md#read-consistency](docs/storage.md#read-consistency-an-implementation-detail-not-a-guarantee)).
- `exists()` now stops at the first visible row instead of walking the full selection —
  measurably cheaper than `count()` when an early match exists, with an identical
  answer on every existing case (empty selection, no match, min-N gated, scope-hidden).

### Changed

- **`Store`'s protocol docstring no longer invites a third-party backend.** It used to
  say any other backend "can too [satisfy the protocol] by simply matching these method
  signatures"; a 2026-09-01 human ruling recorded this seam as **engine-internal** —
  only the three shipped backends (`ObjectStore`, `InMemoryStore`, `PostgresStore`) are
  supported — and the docstring is softened to say so.

  This is **Changed, not Breaking**, checked against every clause
  [docs/compatibility.md](docs/compatibility.md#what-counts-as-a-breaking-change) lists:
  `Store` is neither removed nor renamed in `ontary.__all__` (see the residue below) and
  no method signature changed, so the "obvious" clause is not met; no `Declarations`
  string changed (1); no error code changed meaning or disappeared (2); no
  `EffectMeta`/`AuditEntry`/`EffectRecord`/`OutboxRecord` field was removed or re-meant
  (3); no refusal became permissive or a permissive path started refusing — every one
  of the three backends behaves exactly as it did at 0.8.0 (4); `SCHEMA_VERSION` is
  unchanged at 9 (5); and no descriptor-IR/fingerprint-affecting change was made (6). No
  clause is met.

  The residue, stated plainly because this sentence was the only place the extension
  point was ever promised: `Store` REMAINS in `ontary.__all__` — removing it would
  itself be breaking, and the type is needed in annotations. A reader still meets the
  name at the front door; only the promise that a class of their own could satisfy it
  as a supported backend is withdrawn.

### Migration notes

- None. No path that worked at 0.8.0 stops working and no call site needs to change;
  [`examples/tickets/README.md`](examples/tickets/README.md)'s own migration diary
  records that the storage-envelope work forced no change to the reference app.

## [0.8.0] — 2026-08-26

The removal-and-queries release: remodeling and removal become governed business
operations, and the query surface becomes shape-complete. This is a pre-1.0
minor bump; read the migration notes before upgrading.

```bash
uv add "ontary @ git+https://github.com/ryoochi0112/ontary@v0.8.0"
```

### Added

- `ActionContext.retire(obj_type, obj_id)` and `.unlink(link_api_name, from_id, to_id)` are
  the only handler-facing SDK removal spellings. They run only inside a handler transaction;
  retirement cascade-closes every live link that references the object on the side its link type
  declares for that object type, in that same transaction, and the object and link closures are
  captured in `AuditEntry.writes`.
- `Store.read_last(obj_type, obj_id)` returns an object's newest row whether or
  not it is still live, and `Store.links_from_asof`/`links_to_asof(link_type,
  id, asof)` return the links that were live at a named instant — links closed
  at or after it included. All three on all three shipped backends.
  `read_current` answers `None` both for an id that never existed and for a
  retired one, and `links_from`/`links_to` answer `[]` for an object whose
  links a retirement cascade closed; the action target scope gate needs both
  distinctions. Additive — a custom backend must implement all three.
- All three `Store` backends now provide `retire_object`, `close_link`, and
  `erase_object_content`. Retirement closes the current object row, link closure
  closes one live relationship, and erasure purges content while leaving structural
  tombstone rows intact.
- Source-removal authority is enforced for governed actions: an object type must
  declare whole-type `owned=True` and a link type must declare `owned=True`. An
  undeclared source-backed removal raises `UNDECLARED_SOURCE_REMOVAL`; a cascade
  that reaches such a link rolls back the whole action transaction.
- `where=` now supports the typed operator set `gt`/`gte`/`lt`/`lte`/`in`/`ne`/
  `contains` on every read surface, with catalogued unknown-operator and
  operator-type refusals.
- Consumer object reads accept `order_by` on declared payload fields and are bounded by
  default with `DEFAULT_READ_LIMIT=1000`; pass `limit=None` explicitly for an
  unbounded list. Inside a declared Function, `BoundQuery.list` remains unbounded by
  default and returns a bare list; pass `limit=` when a `Page` is wanted.
- The design guide's CRUD lint now rejects `Remove*` action names alongside
  `Delete*`, keeping removal as a domain business verb.
- `ontary.erase.erase_object()` gives store-holding operators an audited,
  idempotent one-object erasure API. It remains absent from the authoring root,
  `OntologyClient`, `ActionContext`, and MCP surfaces.
- `ontary erase` provides the store-file CLI path for that operator erasure API;
  `--operator` records the supplied identity (or the OS user when omitted),
  successful erasures and coded no-op reports exit zero, and coded refusals exit
  non-zero.
- `GuardedQuery`, `OntologyClient`, and `BoundQuery` now expose visible-row
  `count()` and `exists()` reads, and MCP exposes the same guarded count through
  `count_objects`.
- `aggregate()` and `aggregate_by()` now accept `func="mean"|"count"|"sum"|
  "min"|"max"` on guarded, client, Function, and MCP read surfaces. Every
  reduction retains the existing per-selection/per-group min-N release gate, and
  MCP gains an `aggregate_objects` tool (twelve tools total).
- `traverse(..., reverse=True)` now walks a link from its target side across
  `GuardedQuery`, `OntologyClient`, `BoundQuery`, typed `LinkHandle` calls, and
  MCP. The chosen spelling keeps one traversal method and makes direction an
  explicit call-site choice; human denial for `identity_revealing` links is
  symmetric and occurs before either direction resolves rows.

### Changed

- **Action target scope gate after retirement:** the `scope_semantics="target"`
  gate is now skipped only for a target that was NEVER stored. It used to be
  skipped for anything the store had no live row for, which after this
  release's retirement verb includes a RETIRED object — so a consumer outside
  that object's scope reached the handler with no scope check, and
  `ctx.create_link` (which validates no endpoint) let its writes through.

  <!-- scope-asof-invariant:start -->
  A retired target resolves the scope it owned **as of the instant its own row
  closed**. The links on the chain are read at that instant on both bounds, so
  an edge the object had already left cannot answer for it.

  Where a `ViaLink` hop finds more than one parent at that instant, the first
  parent whose chain resolves wins, over an order the store declares rather
  than each backend's own row order: earliest link `valid_from` first, then
  lowest parent id, compared byte-wise. It is not creation order — `links`
  declares no monotonic key, so two links made in one clock tick are separated
  by id, not by which was written first. Every backend answers in that order
  because it decides which scope owns the object, and therefore which operator
  this gate admits.

  An ancestor object is admitted when it had not already been retired by then —
  but it is read at its newest row, so its payload, and any `DirectProperty`
  key taken off that payload, is the one it carries now: an ancestor updated
  after the instant answers with the scope it is in today, not the one it was
  in then. Making the object side point-in-time too needs an as-of read of an
  object's history, which `Store` does not have. A live target has no closing
  instant and resolves in the present tense, exactly as a consumer read does,
  so this gate loosens nothing for an object that still exists.
  <!-- scope-asof-invariant:end -->

  Outside the resolved scope, `SCOPE_DENIED`; inside it, the handler's own
  precondition refusal as before, including `OBJECT_ALREADY_RETIRED`. It is a
  security fix, not a regression.

  Point-in-time, rather than simply "retired rows allowed", because the two
  obvious readings fail in opposite directions and both are authorization
  defects. Resolving ancestors from the newest row unconditionally lets an
  ancestor retired long ago beat a live one and authorize an operator who no
  longer owns the row. Resolving them live-only refuses an ancestor that was
  alive when the target closed and is the only route to a scope that is alive
  now — denying the operator who does own it, permanently, since a disbanded
  team cannot be un-retired. One instant answers both: at the target's
  `valid_to` the ancestor retired earlier was already closed, and the one that
  outlived the target was not.

  <!-- scope-denied-consequences:start -->
  Where the chain resolves and the consumer covers it, the gate reaches the
  handler's own refusal (`OBJECT_ALREADY_RETIRED`). `SCOPE_DENIED` does not
  mean one thing: it is raised at two places. One is a defense-in-depth refusal
  of a scope-bearing parameter that is not a `str` — parameter validation
  rejects that first, so it is a floor under type confusion rather than a path
  in normal use. The other fires wherever coverage cannot be shown, and that is
  two situations rather than one: the chain resolved and this consumer is
  outside it, which is the ordinary denial this gate does not change; or the
  chain did not resolve at that instant, and deny-by-default denies. Only the
  second belongs to this frame. Four rule kinds are declared, and each meets
  retirement and erasure at its OWN hop:

  - `SelfScope` answers with the object's own id, which neither retirement nor
    erasure takes away.
  - `DirectProperty` reads the scope key off the payload, and
    `erase_object_content` blanks by design what that rule reads, which no
    history-aware read can recover. Retirement leaves the payload alone,
    because this gate reads the newest row rather than the live one, so erasure
    is the only lifecycle event this hop's OWN READ loses to. That is the
    target itself when the target is `DirectProperty`-scoped; it is equally an
    ANCESTOR whose own hop is `DirectProperty`, which denies a `ViaLink`-scoped
    target whose link erasure preserved. Erasure destroying the scope key is
    the point of erasure, not a gap in the gate.
  - `ViaLink` climbs the `links` table, which erasure preserves, so erasure
    costs this hop nothing. It resolves to nothing when none of the parents it
    reaches resolves in turn — among them a parent already retired BEFORE the
    target closed: its edge may still be readable at that instant, but its own
    row is not admitted, so it cannot answer at the one instant this gate asks
    about. One retired after the target — including in the same cascade tick —
    still answers for it.
  - `CustomResolver` is author code handed the raw `Store`, and the engine does
    not reach inside it, so what a retired or erased object resolves to is the
    resolver's own business rather than this frame's. The natural body reads
    `read_current`, which is `None` for a retired object, so a resolver written
    that way denies. A type that needs the precondition refusal after
    retirement declares a second rule — a `DirectProperty` on a scope-key
    column, which survives retirement.

  Each bullet is about one hop, never about one target, and the four are not
  the whole chain. `ScopePolicy.rules` maps each type to an ORDERED list, so a
  target declares as many of these hops as that list holds and is answered by
  the first that resolves; and a level no rule of its own can answer climbs to
  the canonical instance of a narrower scope, where a type declares one — which
  is none of the four. What the engine's own hops share is the frame: any
  object the engine has to read that had already been retired before the target
  closed is refused there, whichever of those hops reached it — so the hop that
  answers a target's level can fail on an object the target's other hops never
  touch. A `CustomResolver` is outside that frame only for the reads its own
  callable makes: the engine does not thread the instant into author code, so
  an ancestor the callable reaches for itself is read however it reads it,
  retired or not. Its ANSWER re-enters the engine, and every object the engine
  reads from there is refused on the frame's own terms — the canonical instance
  a narrower answer names, and any object whose rules the engine goes on to
  ask, whose row is checked before its own resolver runs.

  Every denial in this list fails closed — the object's own owner is denied,
  nothing is disclosed.
  <!-- scope-denied-consequences:end -->

  This history-aware resolution is scoped to that one gate. `resolve_owning_scope`
  takes `include_retired=False` by default and every consumer read keeps it:
  resolving a retired — or erased — row's scope would put its children back in
  a reader's visible set, carrying a population past `min_n` and releasing an
  aggregate computed over the erased subject's own rows.
- **Erasure follows a link endpoint no declared object can own:** a recorded
  link write whose endpoint id matches the object being erased is now treated
  as a reference unless an object of the endpoint's DECLARED type really
  carries that id. `create_link` validates neither endpoint existence nor
  endpoint type, so the declaration alone was letting erasure walk past audit
  and outbox rows that named the erased object. An endpoint an existing
  same-id object of the declared type can own is still left alone.
  It is a security fix, not a regression.
- `Page` and `TypedPage` now refuse `len(page)`, `page[i]`, and `page[i:j]`
  with `PAGE_NOT_ITERABLE`, the refusal the code already documented for them
  and only implemented for iteration. Both previously raised a bare
  `TypeError` naming neither the page nor `.items`. Truthiness is unchanged.
- `Ontology.diagnose()` now reports the empty `contributor_rules` rule list
  that `ScopePolicy.validate` refuses at startup, so the sweep no longer
  returns clean for a declaration that cannot be built.

- **Hidden-field aggregate disclosure guard:** on a consumer surface,
  `aggregate(func="mean"|"count", value_field=<hidden field>)` now raises
  `VISIBILITY_DENIED`; the hidden-field exemption requires an author-declared
  Function over a type declaring `contributor_rules`. This closes the confirmed
  mean/count plus complementary-selection oracle that recovered an individual
  hidden value. It is a security fix, not a regression.
- **Scope-key disclosure guard:** on a hidden `DirectProperty` scope-routing key,
  only bare equality and `in` over an explicit list remain exempt in `where`.
  `gt`/`gte`/`lt`/`lte`/`ne`/`contains`, `in` over a non-list, `order_by`, and
  `group_by` now refuse with `VISIBILITY_DENIED`. This closes the confirmed
  `contains` alphabet-walk oracle (and prevents rank/group-key disclosure). It is
  a security fix, not a regression.
- **min-N contributor counting:** the floor is now measured over the rows that
  carry `value_field`, and rows whose contributor does not resolve count as one
  unknown identity between them rather than one apiece. A type mapped to an
  empty `contributor_rules` list no longer opens the hidden-field exemption, and
  `ScopePolicy.validate` refuses that declaration. This closes the confirmed
  disclosure where retiring one contributor — or closing its links — made
  several rows by that one person look like several people and released their
  mean, and the one where a group of `min_n` people in which only one answered
  released that person's answer as the "mean". It is a security fix, not a
  regression.
- **`where` snapshot and supplied-value rule:** each `where` mapping and each
  condition inside it is now read exactly once, at the public boundary, and every
  gate and the row matcher consume that one snapshot — closing the confirmed
  split where a mapping answering differently on a second read was classified as
  supply-shaped equality and executed as a `contains` alphabet walk. The
  supply-shaped exemption now also requires an operand that actually supplies a
  value (`str`/`int`/`float`/`bool`/`date`), so `where={<hidden key>: None}`,
  `{"in": [None]}`, and `{"in": []}` raise `VISIBILITY_DENIED` instead of acting
  as a free per-row null probe. It is a security fix, not a regression.

### Migration notes

- **Mapping-valued `where` operands:** `where` mappings are now operator syntax,
  so dict equality on a declared `json` property must use the `in` escape, for
  example `where={"data": {"in": [{"kind": "a"}]}}`.
- **Object reads are bounded by default on consumer surfaces:** omitting `limit` from
  `GuardedQuery.get_objects` or `OntologyClient.list` now returns a `Page` bounded by
  `DEFAULT_READ_LIMIT=1000`. Inside a declared Function, `BoundQuery.list` is deliberately
  unbounded by default and returns a bare list; pass `limit=` to opt into a `Page`. Pass
  `limit=None` explicitly for the unbounded list form on consumer surfaces; update callers
  that previously relied on bare consumer reads returning a list.
- **0.8.0 grouped-empty aggregate refusal:** `aggregate_by()` over an empty
  visible selection now raises `MIN_N_VIOLATION`, matching `aggregate()`, instead
  of returning `{}`. Callers that treated `{}` as an empty-result sentinel must
  catch the catalogued visibility refusal instead.
- **`BoundQuery.traverse` legacy `via=` spelling removed in 0.8.0:**
  `BoundQuery.traverse(from_id, via=link_handle)` is removed; use
  `BoundQuery.traverse(link_handle, from_id)`. Passing `via=` now raises a
  natural Python `TypeError` signature error, not a catalogued refusal. Function
  bodies passing `via=` must be updated; mypy will flag typed call sites, but
  untyped/`Any` ones only fail at runtime.

- **`IngestError` now subclasses `OntaryError`:** a handler ordered
  `except OntaryError:` before `except IngestError:` now shadows the ingest
  branch. Code that relied on `IngestError` being outside the hierarchy must
  reorder its handlers. Conversely, `except OntaryError` now catches client-level
  ingest failures that previously escaped.

- **Aggregates near the min-N floor may now refuse:** three counting changes
  that only ever refuse more. An aggregate over an optional `value_field` counts
  only the rows carrying it; unresolved contributors count as one unknown
  identity collectively; and `ScopePolicy.validate` rejects a type mapped to an
  empty `contributor_rules` list — remove the entry to opt out of contributor
  de-duplication. Selections that sat at the edge of `min_n` will start raising
  `MIN_N_VIOLATION`. That number was never backed by `min_n` distinct people.

- **Null `where` operands on a hidden field now refuse:** a bare `None`, an `in`
  over a list containing `None`, and an empty `in` list no longer receive the
  scope-key exemption and raise `VISIBILITY_DENIED`. Null filtering on any
  readable field is unchanged.

- **Function audit scope:** `Declarations.audit_scope` now records that actions are
  always audited, Functions are audited when they declare capabilities unless overridden
  per Function, and a call that releases a hidden field through the contributor exemption
  is audited even when it declares `audit=False`. The latter appends one `kind="function"`
  entry for each call that actually releases.

- **New error codes:** lifecycle and query refusals add
  `OBJECT_ERASURE_NOT_FOUND`, `OBJECT_RETIRE_NOT_FOUND`, `OBJECT_ALREADY_ERASED`,
  `OBJECT_ALREADY_RETIRED`, `LINK_NOT_FOUND`, `STALE_CURSOR`,
  `UNDECLARED_SOURCE_REMOVAL`, `UNKNOWN_OPERATOR`, `OPERATOR_TYPE_MISMATCH`,
  `PAGE_NOT_ITERABLE`, and `GROUP_KEY_COLLISION`. Branch on these stable codes where a
  refusal needs different handling.

## [0.7.0] — 2026-08-25

The DX-suite release: a stricter authoring front door, fail-loud ingest and
query validation, bounded MCP reads, operator diagnostics, and runnable
authoring/serving helpers. This is a pre-1.0 minor bump; read the migration
notes before upgrading.

```bash
uv add "ontary @ git+https://github.com/ryoochi0112/ontary@v0.7.0"
```

### Migration notes from 0.6.0

- **Front door exports (additive):** `ontary.__all__` now includes
  `OntologyClient`, `InMemoryStore`, `EffectMeta`, `EffectDispatcher`,
  `ScopePolicy`, `CustomResolver`, `ref`, `scope_ref`, `RetryPolicy`,
  `DrainReport`, `OutboxRecord`, and `Declarations`. Prefer the replacement
  root imports, for example `from ontary import OntologyClient`; canonical
  submodule imports such as `from ontary.client import OntologyClient` remain
  supported.

- **Ingest now raises by default:** `client.ingest()` and
  `client.ingest_links()` now default to `on_error="raise"`. Catch
  `ontary.ingest.IngestError` and inspect its `report`;
  `len(error.report.inserted_ids)` is the committed-record count. Pass
  `on_error="report"` to retain the old report-returning behavior. The
  committed prefix is still written before a later record fails.

- **`IngestError` is an exception, not a Pydantic model:** it now subclasses
  `Exception` and carries `report` plus the committed-record information;
  replace `isinstance(error, BaseModel)` with `isinstance(error, IngestError)`
  or `except IngestError`. Callers using `model_fields` should inspect the
  explicit exception fields (`index`, `reason`, `code`, and `report`) instead.
  `model_dump()` remains only a limited compatibility serializer for the old
  error-row fields; use `error.report.model_dump()` for the complete batch
  report.

- **Execute signature and result shape:** use `client.execute(params)` or
  `client.execute(params=params)` for a typed action, and
  `client.execute("ActionName", {"field": value})` (or the equivalent
  `action=...`, `params=...` keywords) for the string form. Replace callers
  that depended on the old positional-only parameter names. Action handlers
  may now return nested JSON-safe `dict[str, Any]`; callers that assumed every
  result value was a string should consume the JSON value tree instead.

- **Malformed `execute`/`traverse` calls now refuse as `INVALID_PARAMS`:**
  calling `execute` or `traverse` with a shape no overload accepts (for
  example the string form without params or `from_id`) now raises
  `ValidationFailed` with `code="INVALID_PARAMS"` instead of an
  `InternalError` with `code="INTERNAL_ERROR"`; `INTERNAL_ERROR` is reserved
  for genuine engine invariants.

- **String traversal positional order:** the string form changed from
  `client.traverse(obj_type, obj_id, link)` to
  `client.traverse(obj_type, link, from_id)`. All three arguments are `str`,
  so mypy cannot detect this migration; update positional call sites manually.
  The replacement typed form is `client.traverse(link_handle, from_id)`.

- **`BoundQuery.traverse` legacy `via=` spelling removed in 0.8.0:**
  `BoundQuery.traverse(from_id,
  via=link_handle)` deliberately remains supported, and string
  `BoundQuery.traverse(link, from_id)` remains unchanged. New code may use the
  handle-first `BoundQuery.traverse(link_handle, from_id)` form; treat the
  `via=` spelling as legacy if planning a future breaking cleanup. This legacy
  spelling was removed in 0.8.0; see the 0.8.0 migration note above.

- **Unknown `where` keys now refuse:** typed, string, aggregate, and MCP read
  surfaces validate `where` against declared payload fields. A typo now raises
  `ValidationFailed` with `code="UNKNOWN_FIELD"` instead of returning `[]`.
  Replace typo-tolerant empty-result handling with a declared field name and
  catch/branch on `UNKNOWN_FIELD`; lineage fields remain non-filterable.

- **Constructor and enforcement hardening:** `ScopePolicy` rejects
  `min_n < 1`, empty `levels`, and duplicate levels at construction, so an
  ontology declaration with `scope_levels=[]` is rejected when its policy is
  materialized instead of producing a usable definition. Use a non-empty,
  unique level list and `min_n >= 1`. Registry getters now raise
  `ValidationFailed` instead of bare `KeyError`: handle
  `UNKNOWN_OBJECT_TYPE`, `UNKNOWN_LINK_TYPE`, `UNKNOWN_ACTION`, or
  `UNKNOWN_NAME` as appropriate. Enforcement-path assertion failures are now
  real coded validation/errors, so replace `AssertionError` catches with the
  documented `ValidationFailed`/kind classes. Unknown-link traversal on the
  client and MCP surfaces intentionally remains `UNKNOWN_NAME`.

- **MCP consumer contract:** `query_objects` now defaults to a 100-row page,
  caps pages at 1000, and returns `next_cursor`; pass `limit` and the returned
  cursor as `after` to paginate. The `where` grammar is equality-only on
  payload fields. Tool parameters are validated inside the structured error
  envelope, and all successful results are JSON-normalized; return
  JSON-safe values and handle envelope errors by `code`/`kind`, not raw
  framework text. Grouped-aggregate `MIN_N_VIOLATION` messages now say
  `count withheld`; codes are unchanged, and message text is not a wire
  contract. Read tools carry `readOnlyHint`, while `execute_action` carries
  `destructiveHint` through `ToolAnnotations`. The MCP extra is now pinned to
  `mcp>=1.27.2,<2`.

- **New connector declaration requirement:** concrete `BaseConnector` and
  `SourceConnector` subclasses must define a class-level `name`; missing it
  now fails at class definition. Add `name = "your-source-system"` to the
  subclass (abstract connector bases may still omit it).

- **Fingerprint behavior for new property types:** `date` and `choices` are
  fingerprint-neutral for ontologies that do not use them, so upgrading the
  SDK alone leaves those digests byte-identical. An ontology that declares
  `choices=[...]` moves its digest; declaring a `date` property likewise
  changes that declaration's shape. Follow the [fingerprint acceptance
  procedure](docs/compatibility.md#fingerprints-and-07-property-types),
  including row migration before accepting a new choices constraint.

### Additions

- `Ontology.diagnose()` and `Finding`, including design-guide lint findings.
- Runtime-only explain traces via `explain_read()` and `explain_list()`.
- Deterministic test helpers in `ontary.testing`.
- CLI `validate`, `explain`, `version`, and localhost-only `serve --dev`.
- Five runnable recipes in the [cookbook](docs/cookbook.md).
- Runnable MCP and effects-drain examples under
  [`examples/tickets/`](examples/tickets/).

## [0.6.0] — 2026-08-22

The release that completes the SDK-simplification work. This is a pre-1.0 minor
bump, so the public Python surface and the MCP `error.type` field have breaking
changes even though the store schema does not.

```bash
uv add "ontary @ git+https://github.com/ryoochi0112/ontary@v0.6.0"
```

### Migration from 0.5.0

**0.5.0 was never tagged.** Its `Lineage` → `SourceLineage` migration is part of
the pre-0.6.0 history archived in `github.com/ryoochi0112/ontic`. A consumer coming
from the last published tag, `v0.4.0`, must apply that migration before the 0.6.0
changes. This note does not create or move a git tag.

- **BoundQuery reads:** replace `BoundQuery.get_object(obj_type, obj_id)` with
  `BoundQuery.get(obj_type, obj_id)`, and replace
  `BoundQuery.get_objects(obj_type, where, ...)` with
  `BoundQuery.list(obj_type, where, ...)`. There are no compatibility shims.
  The aggregate methods also gained typed overloads; that is additive and
  non-breaking.

- **`code` is now required when constructing a kind class.** The class-level
  default is gone: `ValidationFailed("msg")` raises `TypeError`, and
  `ValidationFailed("msg", code="INVALID_PARAMS")` is the replacement. Every
  code and its catalogued `kind` are unchanged — this changes only how an
  exception is *constructed*, never what travels on the wire. Passing an empty
  `code` raises `ValueError`.

  If you construct these exceptions yourself, name the code you mean at each
  site; the previous class defaults were `INVALID_PARAMS` (`ValidationFailed`),
  `PRECONDITION_FAILED` (`PreconditionFailed`, `ActionError`),
  `PERMISSION_DENIED`, `VISIBILITY_DENIED`, `AUTHORITY_ERROR`,
  `CARDINALITY_VIOLATION` (`ConflictError`) and `INTERNAL_ERROR`
  (`InternalError`), so passing those preserves today's behaviour exactly. If
  you only *catch* these exceptions, nothing changes.

  Why: a default meant that any construction reached through a rebound name —
  an alias, an assignment, a function parameter, a `for` target, a `getattr` —
  silently shipped the default code instead of the intended one, and the
  consumer branching on `.code` saw the wrong stable value. Five review rounds
  showed that no source-level guard closes this in a language where any name
  can be rebound: each round modelled one more binding shape and the next found
  another. Requiring the argument makes the wrong construction unwriteable
  rather than undetectable, and retired two guards that existed only to chase it.

- **Exception collapse:** the legacy exception names and the `CodedError` alias
  are deleted, not kept as aliases. Catch the kind class shown below and branch
  on its unchanged `code` when the specific refusal matters. `ActionError`
  remains the handler-facing precondition vocabulary.

  - `VisibilityError`: `MinNViolation` → (`VisibilityError`,
    `MIN_N_VIOLATION`); `VisibilityDenied` → (`VisibilityError`,
    `VISIBILITY_DENIED`).
  - `PermissionDenied`: `ActionPermissionError` → (`PermissionDenied`,
    `PERMISSION_DENIED`; also `SCOPE_DENIED`); `Unauthenticated` →
    (`PermissionDenied`, `UNAUTHENTICATED`); `ConsumerUnresolved` →
    (`PermissionDenied`, `CONSUMER_UNRESOLVED`).
  - `PreconditionFailed`: `CapabilityNotProvided` → (`PreconditionFailed`,
    `CAPABILITY_NOT_PROVIDED`); `EffectNotDispatchable` →
    (`PreconditionFailed`, `EFFECT_NOT_DISPATCHABLE`); `FunctionError` →
    (`PreconditionFailed`, `FUNCTION_ERROR`).
  - `ValidationFailed`: `EntityKeyMismatchError` → (`ValidationFailed`,
    `ENTITY_KEY_MISMATCH`); `UndeclaredCapability` → (`ValidationFailed`,
    `UNDECLARED_CAPABILITY`); `UndeclaredEffect` → (`ValidationFailed`,
    `UNDECLARED_EFFECT`); `EffectNotSerializable` → (`ValidationFailed`,
    `EFFECT_NOT_SERIALIZABLE`); `UnknownName` → (`ValidationFailed`,
    `UNKNOWN_NAME`); `UnknownField` → (`ValidationFailed`, `UNKNOWN_FIELD`);
    `OntologyValidationError` → (`ValidationFailed`, `ONTOLOGY_INVALID`);
    `HydrationError` → (`ValidationFailed`, `INVALID_RECORD`);
    `InvalidLimit` → (`ValidationFailed`, `INVALID_LIMIT`);
    `AfterWithoutLimit` → (`ValidationFailed`, `AFTER_WITHOUT_LIMIT`);
    `InvalidGroupBy` → (`ValidationFailed`, `INVALID_GROUP_BY`);
    `NonNumericAggregate` → (`ValidationFailed`, `NON_NUMERIC_AGGREGATE`);
    `ScopePolicyError` → (`ValidationFailed`, `SCOPE_POLICY_ERROR`);
    `UnknownObjectType` → (`ValidationFailed`, `UNKNOWN_OBJECT_TYPE`);
    `UnknownLinkType` → (`ValidationFailed`, `UNKNOWN_LINK_TYPE`);
    `ObjectNotFound` → (`ValidationFailed`, `OBJECT_NOT_FOUND`);
    `InvalidBatch` → (`ValidationFailed`, `INVALID_BATCH`);
    `InvalidCursor` → (`ValidationFailed`, `INVALID_CURSOR`); and
    `DeclaredShapeViolation` → (`ValidationFailed`, `INVALID_RECORD`).
  - `AuthorityError`: the removed store-specific `ontary.store.AuthorityError`
    → (`AuthorityError`, `AUTHORITY_ERROR`); `SourceCreateRefused` →
    (`AuthorityError`, `SOURCE_CREATE_REFUSED`); `UndeclaredSourceWrite` →
    (`AuthorityError`, `UNDECLARED_SOURCE_WRITE`). The `AuthorityError` kind
    class itself is the replacement; it was not removed.
  - `ConflictError`: `StoreBusy` → (`ConflictError`, `STORE_BUSY`);
    `CardinalityViolation` → (`ConflictError`, `CARDINALITY_VIOLATION`);
    `StoreVersionUnsupported` → (`ConflictError`,
    `STORE_VERSION_UNSUPPORTED`); `OntologyDrift` → (`ConflictError`,
    `ONTOLOGY_DRIFT`); `StoreSchemaIncompatible` → (`ConflictError`,
    `STORE_SCHEMA_INCOMPATIBLE`); `CallerTransactionRefused` →
    (`ConflictError`, `CALLER_TRANSACTION_REFUSED`); and `UpcastFailed` →
    (`ConflictError`, `UPCAST_FAILED`).
  - **The old root export `ontary.StoreError` was a base class, not an
    internal-error replacement.** Its fallback code `STORE_ERROR` remains in
    the catalog, but there is no one-to-one kind-class replacement: a store
    catch-all must become `except OntaryError:` (from `ontary.errors`, or the
    root `ontary.OntaryError`; alternatively use an explicit tuple of the kind
    classes below).
    `except InternalError:` is **NOT** equivalent and will not catch the
    concrete store failures. The 13 direct subclasses in the old
    `StoreError` hierarchy now land as follows: `StoreBusy` →
    (`ConflictError`, `STORE_BUSY`); `UnknownObjectType` →
    (`ValidationFailed`, `UNKNOWN_OBJECT_TYPE`); `UnknownLinkType` →
    (`ValidationFailed`, `UNKNOWN_LINK_TYPE`); `ObjectNotFound` →
    (`ValidationFailed`, `OBJECT_NOT_FOUND`); `CardinalityViolation` →
    (`ConflictError`, `CARDINALITY_VIOLATION`); `InvalidBatch` →
    (`ValidationFailed`, `INVALID_BATCH`); `InvalidCursor` →
    (`ValidationFailed`, `INVALID_CURSOR`); `StoreVersionUnsupported` →
    (`ConflictError`, `STORE_VERSION_UNSUPPORTED`);
    `DeclaredShapeViolation` → (`ValidationFailed`, `INVALID_RECORD`);
    `OntologyDrift` → (`ConflictError`, `ONTOLOGY_DRIFT`);
    `StoreSchemaIncompatible` → (`ConflictError`,
    `STORE_SCHEMA_INCOMPATIBLE`); the old store-specific `AuthorityError` →
    (`AuthorityError`, `AUTHORITY_ERROR`); and `CallerTransactionRefused` →
    (`ConflictError`, `CALLER_TRANSACTION_REFUSED`). The inherited
    `SourceCreateRefused` and `UndeclaredSourceWrite` are both
    `AuthorityError` failures, with their specific codes shown above.
  - `CodedError` was an alias, not a distinct code: replace it with the
    `OntaryError` base and inspect the concrete error's `code` and `kind` (the
    base default remains `INTERNAL_ERROR`).

  The authoritative migration rule is that every error `code` and `kind` value
  is **UNCHANGED**; those values are the wire/compatibility surface. The names
  to catch are now `OntaryError` plus `VisibilityError`, `PermissionDenied`,
  `PreconditionFailed`, `ValidationFailed`, `AuthorityError`, `ConflictError`,
  `InternalError`, and `ActionError`.

  The following old dual-inheritance relationships are also gone. Catch the
  kind class and code shown above instead: `UnknownName` was a
  (`ValidationFailed`, `KeyError`) → (`ValidationFailed`, `UNKNOWN_NAME`),
  `UnknownField` was a (`ValidationFailed`, `KeyError`) →
  (`ValidationFailed`, `UNKNOWN_FIELD`), `HydrationError` was a
  (`ValidationFailed`, `ValueError`) → (`ValidationFailed`, `INVALID_RECORD`),
  and `ActionPermissionError` was a (`PermissionDenied`, `ActionError`,
  `PermissionError`) → (`PermissionDenied`, `PERMISSION_DENIED` or
  `SCOPE_DENIED`). At 0.6.0, an existing `except KeyError:` around a typed
  read, `except ValueError:` around hydration, or `except ActionError:` (or
  `except PermissionError:`) around `client.execute(...)` silently stops
  catching these failures: there is no import or type error to expose the
  break.

- **MCP wire change:** in an MCP error payload, `error.type` now carries the
  kind-class name, such as `VisibilityError`, `PermissionDenied`, or
  `ValidationFailed`, instead of the legacy exception-class name.
  `error.code` and `error.kind` remain byte-stable. This is a **BREAKING**
  change for consumers that branch on `error.type`; branch on `code` or `kind`
  for the stable contract.

- **Deleted drift and identity features:** replace `assess_drift` by comparing
  `OntologyFingerprint` values directly, and replace `describe_drift` by
  comparing the fingerprints' `.types` mappings. `DriftAssessment` has no
  replacement object: use `OntologyFingerprint` for the comparison and handle
  the `ConflictError` with code `ONTOLOGY_DRIFT` (the former `OntologyDrift`)
  for the store's refusal. The fingerprint check-and-refuse behavior is
  **UNCHANGED**; only the descriptive-assessment API was removed. The deleted
  `resolve_identities`, `IdentityRule`, and `IdentityMatch` APIs have no SDK
  replacement: matching is caller-owned, and the `ontary.connect.identity`
  module is gone.

- **Front-door demotions:** `ontary.__all__` is exactly 45 names. T10 deleted
  nothing from the engine; names outside that curated front door remain
  importable from their canonical submodule. The complete inventory below is
  copied from `tests/test_docs.py`'s `DEMOTED_NAMES_BY_MODULE`, which is the
  source of truth for both the docs and the guard. Import `MappingValidationError`
  from `ontary.connect`.

  | Destination module | Names still importable there |
  | --- | --- |
  | `ontary.actions` | `ActionExecutor` |
  | `ontary.audit` | `CapabilityAccessRecord`, `EffectRecord` |
  | `ontary.authoring` | `ref`, `scope_ref` |
  | `ontary.client` | `OntologyClient`, `OntologyRuntime` |
  | `ontary.connect` | `LinkSkip`, `MappingValidationError`, `RunReport`, `SourceConnector`, `SourceLineage`, `map_batch`, `run_dlt_extract`, `to_date`, `to_datetime`, `to_optional_date`, `to_optional_datetime` |
  | `ontary.declarations` | `Declarations` |
  | `ontary.effects` | `EffectDispatcher`, `EffectMeta` |
  | `ontary.errors` | `ERROR_CODES`, `ErrorCodeInfo`, `Kind` |
  | `ontary.fingerprint` | `OntologyFingerprint`, `fingerprint_ontology` |
  | `ontary.functions` | `FunctionHandler`, `FunctionRegistry` |
  | `ontary.ingest` | `IngestError`, `IngestReport`, `bulk_link`, `bulk_upsert` |
  | `ontary.mcp_server` | `ConsumerResolver`, `build_multi_consumer_mcp_server` |
  | `ontary.meta` | `ActionParameterDef`, `ActionTypeDef`, `FunctionDef`, `LinkTypeDef`, `ObjectTypeDef`, `OntologyRegistry`, `PropertyDef`, `PropertyType`, `ScopeLevel`, `Upcaster` |
  | `ontary.migrate` | `MigrationFailure`, `MigrationReport`, `migrate_object_type`, `upcast_object_type` |
  | `ontary.ontology` | `OntologyDef` |
  | `ontary.outbox` | `DEFAULT_RETRY_POLICY`, `DrainReport`, `OutboxRecord`, `OutboxState`, `RetryPolicy` |
  | `ontary.query` | `GuardedQuery` |
  | `ontary.scope` | `CustomResolver`, `Direction`, `RowVisibilityFn`, `ScopePolicy`, `ScopeRule`, `resolve_contributor`, `resolve_owning_scope` |
  | `ontary.security` | `ConsumerKind`, `covers_scope` |
  | `ontary.store` | `AuditEntry`, `DEFAULT_BATCH`, `DEFAULT_TENANT`, `InMemoryStore`, `Lineage`, `SCHEMA_VERSION`, `StoredObject`, `WriteRecord`, `accept_ontology_fingerprint`, `check_ontology_fingerprint` |
  | `ontary.upcast` | `upcast_payload` |

- **Docs:** the README narrative is now split into Diátaxis-shaped English
  pages: [`docs/storage.md`](docs/storage.md), [`docs/connectors.md`](docs/connectors.md),
  [`docs/mcp-serving.md`](docs/mcp-serving.md), [`docs/effects.md`](docs/effects.md),
  [`docs/queries.md`](docs/queries.md), and [`docs/authority.md`](docs/authority.md).
  The README is a compact front door; the existing Japanese API-reference and
  ontology-design documents remain the maintained JA surfaces.

### Changed

- **Store compatibility:** the store schema was **NOT bumped**. `SCHEMA_VERSION`
  remains **9**, and existing SQLite store files open with no migration. SQLite
  and Postgres now share one SQL/DDL source; that is an internal consolidation
  with no behavior change, and it is why the Postgres `audit_log` column order
  changed on **FRESH-CREATE schemas only**.

- **The Postgres RLS session GUC is renamed `ontarydk.tenant` → `ontary.tenant`,
  and RLS policies are persisted database state.** A database created by ≤ 0.3.0
  has `tenant_isolation` policies compiled against the old GUC; `_init_schema`
  sees the schema stamp already current (still v9) and will not recreate them.
  Because RLS here is fail-closed, 0.4.0 against such a database reads zero rows.
  Run this once per existing RLS-enabled database before upgrading:

  ```sql
  ALTER POLICY tenant_isolation ON objects
      USING (tenant = current_setting('ontary.tenant', true))
      WITH CHECK (tenant = current_setting('ontary.tenant', true));
  ALTER POLICY tenant_isolation ON links
      USING (tenant = current_setting('ontary.tenant', true))
      WITH CHECK (tenant = current_setting('ontary.tenant', true));
  ALTER POLICY tenant_isolation ON audit_log
      USING (tenant = current_setting('ontary.tenant', true))
      WITH CHECK (tenant = current_setting('ontary.tenant', true));
  ALTER POLICY tenant_isolation ON effect_outbox
      USING (tenant = current_setting('ontary.tenant', true))
      WITH CHECK (tenant = current_setting('ontary.tenant', true));
  ```

Pre-0.6.0 history and tags v0.1.0–v0.4.0 live in github.com/ryoochi0112/ontic.
