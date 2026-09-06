# `ontary` — API reference

**English** · [日本語](api-reference.ja.md) · [← README](../README.md)

The curated front door of `ontary`: **58 names** in `__all__`. The rest of the
engine remains available from its canonical submodule (`ontary.meta`,
`ontary.store`, `ontary.connect`, and so on).

This is a lookup document. For the narrative walkthrough — author, declare, bind,
read, serve — start with the [README](../README.md).

**Contents**

- [Front door](#front-door)
- [Engine surface](#engine-surface)
- [Authoring an ontology](#authoring-an-ontology)
- [Scope policy](#scope-policy)
- [Runtime and clients](#runtime-and-clients)
- [Reading](#reading)
- [Actions](#actions)
- [Functions](#functions)
- [Governed side effects](#governed-side-effects)
- [Security](#security)
- [Stores](#stores)
- [Bulk ingest](#bulk-ingest)
- [`ontary.erase`](#ontaryerase)
- [`ontary.connect`](#ontaryconnect)
- [MCP server](#mcp-server)
- [Descriptor authoring](#descriptor-authoring)
- [Error codes](#error-codes)
- [Exception hierarchy](#exception-hierarchy)

---

## Front door

`__all__` is sorted, duplicate-free, importable, and capped at 58 names. These
are the names an ontology author should reach for without choosing an engine
namespace.

### Authoring vocabulary

`ActionContext`, `ActionParams`, `BaseConnector`, `BoundQuery`, `CanonicalBatch`,
`CanonicalRecord`, `CapabilityHandle`, `Cardinality`, `Consumer`, `DirectProperty`,
`CustomResolver`, `EffectDispatcher`, `EffectHandle`, `EffectMeta`, `EffectPayload`,
`LinkBinding`, `LinkHandle`, `MappingSpec`, `ObjectBinding`, `Ontology`,
`OntologyObject`, `RawTables`, `RowVisibilityStore`, `SelfScope`, `Sensitivity`,
`Source`, `Store`, `ViaLink`, `oid`, `prop`, `ref`, `run_pipeline`, `scope_ref`,
`target`.

### Runtime entries

`Declarations`, `DrainReport`, `Finding`, `InMemoryStore`, `ObjectStore`,
`OntologyClient`, `OutboxRecord`, `Page`, `PostgresStore`, `RetryPolicy`, `ScopePolicy`, `TypedPage`,
`__version__`, `build_mcp_server`, and `declarations`.

### Error classes

`ActionError`, `AuthorityError`, `ConflictError`, `InternalError`, `OntaryError`,
`PermissionDenied`, `PreconditionFailed`, `ValidationFailed`, and
`VisibilityError`.

The 58-name count is asserted by `tests/test_docs.py`, so a
new root export cannot quietly expand this vocabulary.

```python
from ontary import Ontology, OntologyObject, Consumer, prop, target, Cardinality
```

Python 3.12+. The core package depends only on `pydantic`. Extras: `[mcp]` (MCP
server), `[dlt]` (dlt/duckdb connector extraction), `[bq]` (adds BigQuery support).

---

## Engine surface

Names below are intentionally not flattened into the front door. Import them
from the defining submodule when extending the engine or using an advanced
integration. `MappingValidationError` is a plain `Exception` for connector
preflight validation and lives in `ontary.connect`.

### `ontary.actions`

`ActionExecutor`.

### `ontary.audit`

`CapabilityAccessRecord`, `EffectRecord`.

### `ontary.client`

`OntologyRuntime`.

### `ontary.connect`

`LinkSkip`, `MappingValidationError`, `RunReport`, `SourceConnector`,
`SourceLineage`, `map_batch`, `run_dlt_extract`, `to_date`, `to_datetime`,
`to_optional_date`, `to_optional_datetime`.

### `ontary.errors`

`ERROR_CODES`, `ErrorCodeInfo`, `Kind`.

### `ontary.explain`

Operator trace models: `DecisionTrace`, `ScopeRuleTrace`, `ScopePathStep`,
`RedactionTrace`, `MinNTrace`, and `ScanReport`. These are canonical submodule
imports and are not exported from the `ontary` front door.

### `ontary.fingerprint`

`OntologyFingerprint`, `fingerprint_ontology`.

### `ontary.functions`

`FunctionHandler`, `FunctionRegistry`.

### `ontary.ingest`

`IngestError`, `IngestReport`, `bulk_link`, `bulk_upsert`.

### `ontary.erase`

`EraseReport`, `erase_object`.

This is an operator-only Python runbook API. It erases one object by
`(object_type, id)` and is deliberately not in `ontary.__all__`; it is not
reachable from `OntologyClient`, `ActionContext`, or MCP. The operator supplies
`operator=...`. Erasure closes a live object row and purges content from all of
that object's object rows, matching audit parameters, and effect-outbox
payloads, while keeping structural tombstones and appending an audit entry.
Repeating a completed erasure with no newly matched content returns an
`OBJECT_ALREADY_ERASED` no-op report; a repeat with late-arriving content runs the
purge again and reports a real erasure; an unknown object id raises
`OBJECT_ERASURE_NOT_FOUND`.

### `ontary.mcp_server`

`ConsumerResolver`, `build_multi_consumer_mcp_server`.

### `ontary.meta`

`ActionParameterDef`, `ActionTypeDef`, `FunctionDef`, `LinkTypeDef`,
`ObjectTypeDef`, `OntologyRegistry`, `PropertyDef`, `PropertyType`, `ScopeLevel`,
`Upcaster`.

### `ontary.migrate`

`MigrationFailure`, `MigrationReport`, `migrate_object_type`, `upcast_object_type`.

### `ontary.ontology`

`OntologyDef`.

### `ontary.outbox`

`DEFAULT_RETRY_POLICY`, `OutboxState`.

### `ontary.query`

`GuardedQuery`.

### `ontary.scope`

`Direction`, `RowVisibilityFn`, `ScopeRule`, `resolve_contributor`,
`resolve_owning_scope`.

### `ontary.security`

`ConsumerKind`, `covers_scope`.

### `ontary.store`

`AuditEntry`, `DEFAULT_BATCH`, `DEFAULT_TENANT`, `Lineage`,
`SCHEMA_VERSION`, `StoredObject`, `WriteRecord`, `accept_ontology_fingerprint`,
`check_ontology_fingerprint`.

### `ontary.testing`

Public SDK-user test helpers are `make_store`, `consumer`, `raises_code`,
`capture_effects`, `FixedClock`, and `SequentialIds`. `make_store(ontology)`
creates a fresh `InMemoryStore`; `consumer(...)` builds a valid `Consumer`; and
`raises_code(code)` asserts a raised error by its machine-readable code — any
`OntaryError`, including `ontary.ingest.IngestError`, as well as structurally
compatible author-defined coded exceptions that expose a stable string `.code`.
`capture_effects()` returns a callable dispatcher whose `.effects` list records
`(payload, meta)` pairs without outside delivery. `FixedClock(start)` returns
the same timezone-aware datetime on every call and rejects a naive start.
`SequentialIds(prefix)` returns deterministic IDs `prefix-1`, `prefix-2`, and
so on.

### `ontary.upcast`

`upcast_payload`.

---

## Authoring an ontology

### `Ontology(name, scope_levels, min_n=3)`

The authoring facade. Accumulates an `OntologyRegistry`, a `ScopePolicy`, and the
declared handlers, then hands out runtimes.

| Parameter | Type | Notes |
| --- | --- | --- |
| `name` | `str` | Also the default MCP server name. |
| `scope_levels` | `list[str]` | Your own hierarchy, coarsest last — e.g. `["queue", "org"]`. There are no built-in levels. |
| `min_n` | `int` = `3` | Minimum distinct contributors for any aggregate. |

Declaration methods, all decorators except `link`:

| Method | Purpose |
| --- | --- |
| `@ontology.object(...)` | Register an `OntologyObject` subclass as an object type |
| `ontology.link(api_name, from_cls, to_cls, cardinality, ...)` | Register a link type; returns a `LinkHandle` |
| `@ontology.action(params_cls, ...)` | Register a typed action handler |
| `@ontology.function(...)` | Register a derived-value function |
| `ontology.capability(proto, ...)` | Declare a capability; returns a `CapabilityHandle` |
| `ontology.effect(payload_cls, ...)` | Declare an effect; returns an `EffectHandle` |
| `ontology.validate()` | Validate and **freeze** registration |
| `ontology.bind(store, ...)` | Build an `OntologyRuntime` |

`validate()` (and touching `.definition`) freezes the ontology — any later
`object`/`link`/`action`/`function` call raises. Call it once, after every
declaration.

#### `@ontology.object(*, layer, owned=False, api_name=None, description=None, display_name=None, scope=None, contributor=None, row_visibility=None)`

Registers the decorated `OntologyObject` subclass.

- **`layer`** — your own grouping string (`"L0"`, `"core"`, whatever). No engine
  behavior attaches to it; it is carried through to MCP introspection
  (`list_object_types`) so agents and tooling can see how you grouped things.
- **`owned`** — `True` marks the whole type ontology-owned (no source may write it);
  a `dict` marks specific properties owned with their defaults, e.g.
  `owned={"escalated": False}`.
- **`scope`** — `"unscoped"`, or a list of scope rules (see
  [Scope policy](#scope-policy)). Resolution runs **per declared level**: for each
  level, rules are tried in declaration order and the first that both targets that
  level and resolves to a value wins.
- **`contributor`** — scope-rule list identifying the *person* behind a row, used for
  min-N counting.
- **`row_visibility`** — `(store, consumer, obj_id, payload) -> bool`, an extra
  per-row gate applied on top of scope.

#### `ontology.link(api_name, from_cls, to_cls, cardinality, *, description=None, identity_revealing=False, owned=False) -> LinkHandle`

`cardinality` is a `Cardinality` enum member or its string name:
`ONE_TO_ONE`, `ONE_TO_MANY`, `MANY_TO_ONE`, `MANY_TO_MANY`.

`identity_revealing=True` means a **human** consumer is refused the traversal
outright (AI consumers may still follow it). The returned `LinkHandle` is what you
pass as the first argument to `client.traverse(link_cls, from_obj_or_id)` for a
typed result.

#### `OntologyObject`

Base class for declared object types — a `pydantic.BaseModel` subclass, so
validators, computed fields, and plain annotated attributes all work normally.

Two attributes are attached by the engine on a read, and are **not** model fields
(so they never collide with a property you declared):

| Attribute | Type | Meaning |
| --- | --- | --- |
| `lineage` | `Lineage \| None` | Where the row came from and its validity window |
| `redacted_fields` | `frozenset[str]` | Properties blanked for *this* consumer |

`redacted_fields` is what distinguishes "hidden from you" from "genuinely stored as
`None`" — a visible-but-absent optional field is `None` too, but never appears here.

#### `prop(*, primary_key=False, sensitivity=None, scope_level=None, required=None, property_type=None, **field_kwargs)`

`pydantic.Field(...)` plus ontology metadata. Unrecognized kwargs pass straight
through to `Field`, so `prop(default=None, description="...")` behaves as expected.
`prop()` is never a parallel field system: bare annotated fields and plain `Field(...)`
keep working on the same class.

> **A property with restricted `sensitivity` must be declared `X | None`.** The
> decorator raises a coded validation error at class-registration time otherwise —
> because a redacted read has to be able to return `None` for it.

#### Field markers: `ref`, `target`, `scope_ref`

All take an `OntologyObject` subclass and pass extra kwargs to `Field`.

| Marker | Declares |
| --- | --- |
| `ref(cls)` | This param refers to an object of `cls` |
| `target(cls)` | …and it is the action's **target** — scope is enforced against it |
| `scope_ref(cls)` | …and it names the **scope** the action creates within |

These drive `refers_to` / `scope_semantics` on the generated `ActionParameterDef`, so
scope enforcement is declared by the author rather than hardcoded in the engine.

#### `ActionParams`

Base class for typed action-params models. Fields use the markers above.

```python
class EscalateTicketParams(ActionParams):
    ticket_id: str = target(Ticket)
    reason: str | None = None
```

---

## Scope policy

Scope is **declared**, never inferred. Four rule types compose into a `ScopePolicy`;
you normally never build one by hand — `@ontology.object(scope=[...])` does it.

| Rule | Fields | Resolves the level from |
| --- | --- | --- |
| `SelfScope` | `level` | The object's own id |
| `DirectProperty` | `level`, `property_name` | A property on the row |
| `ViaLink` | `link_api_name`, `direction` (`"from"`/`"to"`), `parent_type` | Following a link to a parent, recursively |
| `CustomResolver` | `level`, `fn(store, obj_type, obj_id) -> str \| None` | Arbitrary caller logic |

### `ScopePolicy`

| Field | Type | Notes |
| --- | --- | --- |
| `levels` | `list[str]` | |
| `unscoped_types` | `set[str]` | A type here must NOT also appear in `rules` -- `validate()` and every read refuse the overlap with `SCOPE_POLICY_ERROR`. |
| `rules` | `dict[str, list[ScopeRule]]` | |
| `contributor_rules` | `dict[str, list[ScopeRule]]` | |
| `row_visibility` | `dict[str, RowVisibilityFn]` | |
| `min_n` | `int` | |

### Resolution helpers

```python
resolve_owning_scope(policy, store, obj_type, obj_id) -> dict[str, str | None]
resolve_contributor(policy, store, obj_type, obj_id) -> str | None
covers_scope(policy, consumer, resolved) -> bool
```

`resolve_owning_scope` returns one entry per declared level. `covers_scope` **denies
when the consumer's level is unresolved** — scope enforcement never fails open.

`ValidationFailed` (`SCOPE_POLICY_ERROR`) is raised when a rule references an
undeclared object type, link type, or level.

---

## Runtime and clients

```python
runtime = ontology.bind(store, capabilities={...}, effects={...})   # once
client  = runtime.for_consumer(consumer)                            # cheap, per request
```

### `Ontology.bind(store, *, clock=None, id_factory=None, capabilities=None, effects=None)`

`clock` is a callable returning a timezone-aware `datetime`; it defaults to
`datetime.now(timezone.utc)`. `id_factory` is a callable returning `str`; it
defaults to UUID-shaped IDs. Both seams are stored on the shared runtime and
are inherited by every `for_consumer()` view. Explicit `drain_effects(now=...)`
still takes precedence over the runtime clock.

### `OntologyRuntime(ontology, store, handlers=None, *, clock=None, id_factory=None, capabilities=None, effects=None)`

Shared, consumer-free machinery for one `(ontology, store)` pair — query layer,
action executor, bound handlers — wired exactly once.

- **`.for_consumer(consumer, *, capabilities=None, effects=None) -> OntologyClient`** —
  a cheap view. Serving many consumers from one process never re-wires anything.
- **`.explain_read(consumer, obj_type, id) -> DecisionTrace`** — explains one
  guarded read, including every evaluated scope rule and resolution path,
  sensitivity redactions, the final verdict (`visible`, `redacted`, `denied`, or
  `not_found`), and the error code a real read would raise, if any.
- **`.explain_list(consumer, obj_type, where=None) -> list[DecisionTrace]`** —
  returns one trace per raw matching row, including denied rows. Every trace also
  carries the selected visible population's aggregate-relevant min-N outcome;
  this is diagnostic context and does not add a min-N gate to ordinary lists.

> **Operator trust warning.** Explain reveals hidden-row existence; grant access
> to it only at the same trust level as holding the raw store. It is deliberately
> absent from `OntologyClient` and is never registered as an MCP tool.

Import the frozen result model from its canonical submodule:

```python
from ontary.explain import DecisionTrace
```

`DecisionTrace.rules` contains `ScopeRuleTrace` entries with rule kind,
requested level, match result, resolved scope id, and a `scope_path` of object
type/id hops (ViaLink edges name the link and direction). `redactions` names the
fields removed and the consumer kind. `min_n` is `not_applicable` for a single
read and `passed`/`failed` for an explained list selection; its count is included
because the entire surface is operator-only.

### `OntologyClient(ontology, store, consumer, *, capabilities=None, effects=None)`

Bound to exactly one `(ontology, store, consumer)`. Constructing it directly works
and builds a single-use runtime internally.

| Method | Returns |
| --- | --- |
| `.get(obj_type, obj_id)` | `T \| StoredObject \| None` |
| `.list(obj_type, where=None, *, limit=DEFAULT_READ_LIMIT, after=None, order_by=None)` | `list[T] \| list[StoredObject] \| TypedPage[T] \| Page` |
| `.traverse(obj_type, link, from_id, *, reverse=False)` or `.traverse(link_cls, from_obj_or_id, *, reverse=False)` | `list[T] \| list[StoredObject]` |
| `.aggregate(obj_type, value_field, where=None, *, func="mean")` | `float \| int` |
| `.aggregate_by(obj_type, value_field, group_by, where=None, *, func="mean")` | `dict[str, float \| int]` |
| `.count(obj_type, where=None)` | `int` |
| `.exists(obj_type, where=None)` | `bool` |
| `.count_contributors(obj_type, where=None)` | `int` |
| `.execute(params)` or `.execute(action, params)` | `dict[str, Any]` |
| `.call_function(api_name, params)` | `Any` |
| `.ingest(obj_type, records, source, *, on_error="raise")` | `IngestReport` (raises `IngestError` when failures exist) |
| `.ingest_links(link_api_name, pairs, source, *, on_error="raise")` | `IngestReport` (raises `IngestError` when failures exist) |

**Typed vs. dynamic.** Pass the author's *class* to get typed instances back
(`client.get(Ticket, id) -> Ticket | None`, mypy-inferred, zero casts). Pass a
*string* to get `StoredObject`s — the dynamic form, which is also MCP's wire shape
and what generic tooling uses. `.traverse` takes a `LinkHandle` first for the typed form:
`client.traverse(link_cls, from_obj_or_id)`.

Every call's `where` / `order_by` / `group_by` / `value_field` keys are checked
against the type's declared properties, on the string form as well as the typed
one: an unknown key raises `UNKNOWN_FIELD` rather than silently matching nothing
or returning a number. This includes a key that some stored row happens to carry
— an undeclared payload key is allowed on write, but it is not addressable by
name in a read. A *hidden but declared* key is refused with
`VisibilityError` and `VISIBILITY_DENIED` when the operation would disclose it;
the narrow scope-routing and Function-aggregate exceptions are described under
Filters and Aggregates below.

For object reads, omitting `limit` applies `DEFAULT_READ_LIMIT=1000` and returns
a `Page` (or `TypedPage` for a typed call). Passing `limit=None` explicitly
selects the unbounded list form; a positive integer selects a page. `order_by`
accepts a declared payload field (ascending by default) or a
`(field, "asc"|"desc")` pair. Unknown fields raise `UNKNOWN_FIELD`; hidden fields
are refused with `VISIBILITY_DENIED`, even when they are `DirectProperty`
scope-routing keys. Unlike the equality-shaped `where` exemption below,
`order_by` always uses the un-exempted hidden-field gate.

**Two asymmetries between the surfaces**, both easy to trip over:

> **Hydration.** A typed read parses a `datetime`-typed property into a real
> `datetime` (via Pydantic); the string surface returns the ISO-8601 string exactly as
> stored. The store never changes what it persists — only the typed client's hydration
> step parses on the way out.

> **Redaction shape.** A redacted field comes back as `None` on the typed surface, with
> its name in `redacted_fields`. On the **string and MCP surfaces the key is absent
> from `payload` entirely** — there is no `redacted_fields` companion there. Read
> defensively over MCP (`payload.get("email")`, not `payload["email"]`), and do not
> infer "not stored" from a missing key: it may simply be hidden from you.

---

## Reading

### `StoredObject`

| Field | Type |
| --- | --- |
| `payload` | `dict[str, Any]` |
| `lineage` | `Lineage` |

A frozen payload/lineage split — never a bare dict with smuggled `_object_type`-style
keys.

### `Lineage`

`object_type`, `object_id`, `valid_from`, `valid_to`, `source_system`, `source_id`,
`extracted_at`.

### Filters, ordering, and bounded reads

`where` accepts a bare scalar for equality or a one-key mapping with one of
`gt`, `gte`, `lt`, `lte`, `in`, `ne`, or `contains`. Comparisons apply to
declared `int`, `float`, or `date` properties; `in` takes a list of declared
values, `ne` applies to every declared type, and `contains` is a substring test
for `str`. Unknown operators raise `UNKNOWN_OPERATOR`; an operator or operand
that does not match the declared property type raises
`OPERATOR_TYPE_MISMATCH` — including the bare scalar, which is the only equality
spelling. `None` is the exception: it is the null test, selecting rows where the
property is absent. An `int` operand on a declared `float` is widening, not a
mismatch. Mapping-valued `where` is the same grammar on typed,
string, aggregate, Function, and MCP read surfaces.

For a hidden property declared as a `DirectProperty` scope-routing key, the
scope-key exemption applies only to a bare `eq` value and `in` over an explicit
list. `gt`, `gte`, `lt`, `lte`, `ne`, and `contains`, plus `in` over a non-list or
any malformed shape, are refused with `VISIBILITY_DENIED` because they let the
caller learn a value. The operand must also actually supply a value — a `str`,
`int`, `float`, `bool`, or `date` — so `None`, a list containing `None`, and an
empty `in` list are refused too: a bare null needs no prior knowledge and would
be a per-row null probe. Null filtering on a readable field is unchanged. Each
`where` mapping, and each condition inside it, is read exactly once at the public
boundary; every gate and the row matcher consume that one snapshot, so a mapping
that answered differently on a second read cannot be classified as one predicate
and executed as another.

`where` must be a mapping or omitted. Anything else — a string, a list, a number
— raises `INVALID_PARAMS` on every read surface, including a *falsy* one: `""`,
`0`, `[]`, and `False` are refused rather than treated as "no filter". An empty
**mapping** `{}` still means no filter, as does `None`.

`order_by` accepts a declared payload field, ascending by default, or a
`(field, "asc"|"desc")` pair. Unknown fields raise `UNKNOWN_FIELD`; hidden fields
are refused with `VISIBILITY_DENIED`, including hidden scope-routing keys, because
the returned rank discloses the value. `group_by` has no scope-key exemption
either: its returned dictionary key discloses the group value. Lineage fields are
not payload fields.

On the consumer surfaces (`OntologyClient` and `GuardedQuery`), omitting `limit`
applies `DEFAULT_READ_LIMIT=1000` and returns a `Page` (or `TypedPage` for a typed
call). Pass `limit=None` explicitly for the unbounded list form. Inside a declared
Function, `BoundQuery.list` is unbounded when `limit` is omitted and returns a bare
list; pass a positive `limit` for a `Page`. Pagination composes with `order_by` and
`after` on the bounded surfaces.

### Pagination — `Page` / `TypedPage[T]`

Both carry `items` and an opaque `next_cursor: str | None`. On the consumer surfaces,
omitting `limit` returns a page using the default bound above, while `limit=None` is
the explicit unbounded-list opt-in. Inside a declared Function, `BoundQuery.list`
returns a bare list when `limit` is omitted or `None`; passing a positive `limit`
returns a page.

The contract:

- **Exact-size pages.** A page holds exactly `limit` items whenever that many visible
  rows remain — filtering downstream of the store read can never shorten a page. A
  short page always means "no more rows", never "some were hidden from you".
- **The cursor is opaque.** A random per-row token. Not a row id, not a count, not an
  order. Never parse it; store it and pass it back to `after=`.
- **`next_cursor is None` means exhausted-before-full.** A page that fills exactly at
  the last row still carries a cursor; the follow-up call returns one final empty
  page. So `while next_cursor is not None` always terminates correctly rather than
  stopping a page early.
- **During an unordered walk, a mid-walk update may repeat a row but never skip
  one.** The store is close-old-insert-new. During an ordered walk, a row whose
  sort value moves ahead of the cursor is silently dropped, while a row whose
  sort value moves behind it is duplicated. Only a quiescent ordered walk is
  gap-free and repeat-free.
- `after` without `limit` → `AFTER_WITHOUT_LIMIT`. `limit < 1` → `INVALID_LIMIT`.

### Aggregates

`aggregate(...)` and `aggregate_by(..., group_by=...)` accept
`func="mean"|"count"|"sum"|"min"|"max"`; the default remains `"mean"`.
Ungrouped aggregation returns a plain numeric value: `count` is an `int` and
the other functions return `float`. Grouped aggregation returns the corresponding
value per group in a `dict`.

Both enforce hidden-field checks on `where`/`group_by`, min-N over **distinct
contributors**, `UNKNOWN_FIELD` for a `value_field` the type does not declare, and
`NON_NUMERIC_AGGREGATE` for a declared but non-numeric one (both checked before
any row is read). A hidden `value_field` may be aggregated only from an
author-declared Function, over a type declaring `contributor_rules`, and only with
`func="mean"` or `func="count"`. From a consumer surface — client, typed, or MCP —
that operation raises `VisibilityError` with `VISIBILITY_DENIED`; `sum`, `min`, and
`max` remain refused. Every function uses the same min-N release discipline. An
empty visible selection raises
`MIN_N_VIOLATION`; `aggregate_by` also refuses the grouped-empty `{}` case
instead of returning an empty dictionary.

`group_by` is validated like every other field name on this surface, on **all**
forms rather than only the typed one: a name the type does not declare raises
`UNKNOWN_FIELD`, exactly as `where` keys and `order_by` do, instead of matching
nothing and returning the ungrouped value under the key `"None"`. A falsy
`group_by` is never a silent collapse to the ungrouped path either — the code
still differs there by surface: `INVALID_GROUP_BY` on the string form and
`BoundQuery`, `UNKNOWN_FIELD` on the typed form (the class-property check runs
first).

A `json`-declared `group_by` raises `INVALID_GROUP_BY`: its values may be a
`dict` or `list` and so need not be hashable. The **declared type** is what is
checked, not the stored values, so a `json` property that happens to hold only
scalars refuses too rather than working until the first `dict` arrives.

Two distinct group values that release as the same dictionary key raise
`GROUP_KEY_COLLISION` — most often an optional property, which keys `None` on
the rows that lack it and collides with a row carrying the literal string
`"None"`. One released cell can only describe one population. The check runs per
group as each is released, after that group's min-N test, so a selection that
min-N would withhold still raises `MIN_N_VIOLATION`.

`aggregate`, `aggregate_by`, and `count_contributors` refuse an unregistered
object type with `UNKNOWN_OBJECT_TYPE`, matching what the same calls have always
done when a `where=` is present.

### `GuardedQuery(store, registry, policy)`

The engine-level read path, taking an explicit `consumer` per call:
`.get_object`, `.get_objects`, `.traverse`, `.aggregate`, `.aggregate_by`,
`.count`, `.exists`, and `.count_contributors`. Most
callers use `OntologyClient` instead.

---

## Actions

An action is a typed params class plus a handler decorated with
`@ontology.action(params_cls, target=..., roles=[...], capabilities=(), effects=())`.

Every `execute` runs the same pipeline:

**registered?** → **role permitted?** → **scope covers the declared target/scope
param?** → **preconditions** → **transactional side effects** → **append-only audit**

Every attempt is audited — `ok`, `denied`, and `error` alike.

### `ActionContext`

What a handler receives instead of a raw store handle. Writes are auto-stamped with
the action's own `Source`.

| Member | Purpose |
| --- | --- |
| `.insert(obj_type, payload) -> str` | Create |
| `.update(obj_type, obj_id, changes)` | Update |
| `.create_link(link_api_name, from_id, to_id)` | Link |
| `.retire(obj_type, obj_id)` | Retire the object and cascade-close every live link that references the object on the side its link type declares for that object type |
| `.unlink(link_api_name, from_id, to_id)` | Close one live link |
| `.read_current(obj_type, obj_id) -> StoredObject \| None` | Read |
| `.read_all(obj_type) -> list[StoredObject]` | Enumerate current rows |
| `.links_from(link_api_name, from_id) -> list[str]` | Traverse |
| `.links_to(link_api_name, to_id) -> list[str]` | Traverse |
| `.capability(handle) -> P` | Fetch a declared capability |
| `.emit(payload)` | Emit a declared effect |
| `.consumer` | The calling `Consumer` |

`read_current` and `read_all` are trusted handler reads: raw, unredacted, and
unscoped. They are intentionally not guarded consumer queries. In particular, filtering
an enumeration by scope or sensitivity could hide an existing row from an id allocator
and cause id reuse.

`retire` and `unlink` are the only handler-facing SDK removal operations. They may be called only
inside the engine-owned action transaction. `retire` closes the object's current
row and cascade-closes every live link that references the object on the side its link type
declares for that object type, in that transaction; the object and every link closure are captured in
`AuditEntry.writes`. `unlink` closes one
matching live link.

Raise `ActionError` for a failed precondition. `code` is required (0.6.0):
pass `PRECONDITION_FAILED`, or your own stable code.

### Authority

Writes are refused unless declared. An action creating an object of a type that is
not whole-type `owned=True` raises `SOURCE_CREATE_REFUSED`; updating a source-backed
property, or creating a source-backed link, that is not declared ontology-owned
raises `UNDECLARED_SOURCE_WRITE`. Source data and ontology-owned state stay separable.

Removal uses the same authority boundary: `retire` requires the whole object type
to declare `owned=True`, and `unlink` requires the link type to declare `owned=True`.
Otherwise the action raises `UNDECLARED_SOURCE_REMOVAL`. If an object retirement
cascade reaches a source-backed link, the whole action transaction rolls back,
including earlier link closures.

`execute()` called while the caller already holds a store transaction is refused
(`CALLER_TRANSACTION_REFUSED`, not audited) — otherwise an applied-and-audited action
could be rolled back underneath the audit log.

### `AuditEntry`

`ts`, `actor`, `role`, `action`, `target_type`, `target_id`, `params`, `outcome`,
`invocation_id`, plus integrity records: `writes: list[WriteRecord]`,
`effects: list[EffectRecord]`, `capability_accesses: list[CapabilityAccessRecord]`.

**`kind: Literal["action", "function"]`** — what produced the entry. Actions and
functions share one log; `kind` is how a reader tells them apart, since nothing stops
an ontology declaring an action and a function with the same `api_name`. On a
`function` entry, `action` holds the function's api_name, `target_type` is `""` (a
function has no target object type), and `writes`/`effects` are always empty.

**`invocation_id: str | None`** — one id per `execute()` (or audited
`call_function()`) call, stamped on *every* entry that call writes: the
`denied`/`error`/`ok` entry and, for an action with effects, the later
`effects_dispatched` entry. Correlate a `pending` effect with its
outcome by this value rather than by matching fields and append order — two calls to
the same action with the same params are otherwise indistinguishable. `None` means
the entry predates the field (a store file written by an older engine); it is never
invented for such rows.

- `WriteRecord` — `op` (`create`/`update`/`link`), `object_type`, `link_type`,
  `object_id`, `from_id`, `to_id`
- `EffectRecord` — `api_name`, `payload`, `outcome` (`pending`/`dispatched`/`failed`), `error`
- `CapabilityAccessRecord` — `api_name`, `count`

---

## Functions

A function is `(query: BoundQuery, params: dict) -> Any`, decorated with
`@ontology.function(...)`. No store handle ever reaches it — only already-guarded
reads. Functions return derived values and never write.

### `BoundQuery`

A `GuardedQuery` with the consumer fixed: `.get`, `.list`, `.count`, `.exists`, `.traverse`,
`.aggregate`, `.aggregate_by`, `.count_contributors`,
`.capability(handle)`. Typed overloads
work the same as on `OntologyClient` (`query.get(Ticket, id) -> Ticket | None`).

Function *params* stay `dict[str, Any]` on both surfaces — typed function params are
not part of this API.

`PreconditionFailed` (`FUNCTION_ERROR`) covers an undeclared api_name, a duplicate
registration, or no bound handler.

### The function audit boundary

For an audited call, `OntologyClient.call_function` appends one `kind="function"` audit entry —
carrying the invocation id, the params, the outcome, and the handler's
`capability_accesses`. `writes` and `effects` are empty by construction.

Whether a function is audited is **conditional**, via `FunctionDef.audited`:

| Declaration | Audited? |
| --- | --- |
| declares capabilities | ✅ yes (default) |
| declares none | ❌ no (default) |
| `@ontology.function(audit=True)` | ✅ yes, always |
| `@ontology.function(audit=False)` | ❌ no — except a release, below |

Functions run far more often than actions, and one that declares no capability
cannot reach outside the process — everything it reads is already bounded by the
guarded query layer. Auditing every call would be write amplification for little
gained, so the default records the case an auditor actually asks about.

The error path is audited too: a handler that reached outside and then raised has
already had its effect on the world.

**A hidden-field release is audited whatever the declaration says.** The guarded
query layer's bound includes the AC10 contributor exemption, so a capability-less
function can hand a consumer a `mean` or `count` over a field they cannot read
themselves. When a call actually does that, `call_function` appends its entry —
`audit=False` included, because an ontology should not be able to opt out of
recording that it released an individual-bearing number.

The trace follows the **release**, not the declaration:

| The call | Audited? |
| --- | --- |
| released a hidden field through the exemption | ✅ yes, whatever was declared |
| aggregated a field the consumer could read anyway | per the table above |
| opened the exemption but min-N refused | per the table above — nothing was released |
| declaring `contributor_rules` anywhere in the ontology | per the table above — declaring is not releasing |

Two reasons to audit one call still append one entry.

A bare `FunctionRegistry.call` has **no** boundary — the guarantee belongs to the
client surface, the same way the write gate belongs to `execute()` rather than to the
store.

---

## Governed side effects

Anything a handler wants from the outside world must be **declared**, then provided
at bind time. Undeclared use is refused, and every use is audited.

### Capabilities — things a handler reads or calls

```python
Clock = ontology.capability(ClockProto, name="clock")

@ontology.action(P, target=T, roles=["Agent"], capabilities=[Clock])
def handler(ctx, params):
    now = ctx.capability(Clock).now()
```

Requesting an undeclared capability raises `UNDECLARED_CAPABILITY`; a declared one
with no provider bound raises `CAPABILITY_NOT_PROVIDED`.

### Effects — things a handler wants to happen

```python
Notify = ontology.effect(NotifyPayload, api_name="Notify")

@ontology.action(P, target=T, roles=["Agent"], effects=[Notify])
def handler(ctx, params):
    ctx.emit(NotifyPayload(...))
```

Effects are **data, not calls**: the handler emits a payload, and dispatch happens
outside the transaction. `EffectMeta` (`action`, `actor_id`, `role`, `ts`, `effect_id`,
`attempt`) accompanies each. Emitting an undeclared effect raises `UNDECLARED_EFFECT`; a
declared one with no dispatcher raises `EFFECT_NOT_DISPATCHABLE`; a payload that cannot
be JSON-encoded for the outbox raises `EFFECT_NOT_SERIALIZABLE` *inside* the transaction,
so the action rolls back and nothing is sent.

Providers are bound at `ontology.bind(store, capabilities={...}, effects={...})` or
per client via `for_consumer(...)`.

### Durable delivery

An emitted effect is written to the `effect_outbox` table inside the action transaction,
so the work item commits with the ontology writes. Delivery is **at-least-once**.

| Surface | Signature | Notes |
| --- | --- | --- |
| `RetryPolicy` | `RetryPolicy(max_attempts=3, initial_backoff=1s, multiplier=2.0, max_backoff=5m, lease=60s)` | Bound per runtime/client via `effect_retry=`. `max_attempts=1` reproduces the old at-most-once behavior. Backoff is deterministic — no jitter. |
| `OntologyClient.drain_effects` | `drain_effects(*, limit=100, now=None) -> DrainReport` | Claims due rows (leased, so two drainers do not double-send), attempts each with this client's dispatchers, returns `DrainReport(claimed, delivered, retrying, failed, skipped)`. Also on `OntologyRuntime`. |
| `OntologyClient.outbox` | `outbox() -> list[OutboxRecord]` | Administrative read of every row — `pending`, `delivered`, and terminally `failed` alike. |

The post-commit pass inside `execute()` is attempt 1 of the policy. Nothing retries on a
schedule of its own: **the SDK starts no threads**, so a deployment that wants recovery
must call `drain_effects()` from a worker, a cron job, or a request tail. A row whose
effect this client has no dispatcher for is released, not failed, and counted in
`skipped`.

Because a row is marked `delivered` *after* the outside call returns, a crash in between
redelivers. **A dispatcher must be idempotent on `EffectMeta.effect_id`** — one stable id
per emission, unchanged across attempts. When `max_attempts` is spent the row becomes
`failed`: a terminal dead letter, kept for reading, never retried again.

---

## Security

### `Consumer`

| Field | Type |
| --- | --- |
| `actor_id` | `str` |
| `role` | `str` |
| `scope_level` | `str` — one of the ontology's declared levels |
| `scope_id` | `str` |
| `kind` | `Literal["human", "ai"]` |

Four mechanisms, all enforced in the engine:

1. **Scope visibility** — resolved through the declared `ScopePolicy`; an unresolved
   level denies.
2. **Sensitivity redaction** — `Sensitivity(ai_usable, human_visible)` per property.
   A hidden field reads back `None` and is named in `redacted_fields`.
3. **min-N** — aggregates over fewer than `min_n` distinct contributors raise
   `VisibilityError` with code `MIN_N_VIOLATION`. Contributors come from the declared
   `contributor` rules, so
   repeated rows from one person do not clear the bar. The count is taken over the
   rows that carry `value_field`, not every row in the selection — a group of
   `min_n` people in which only one answered does not release that answer. Rows
   whose contributor does not resolve count as one unknown identity between them,
   never one apiece, so closing a link or retiring a contributor cannot turn one
   person's rows into a releasable population.
4. **Identity-revealing links** — refused for human consumers before any target is
   resolved.

`covers_scope(policy, consumer, resolved) -> bool` is the single coverage rule, shared
by the read path and the write path.

---

## Stores

### The `Store` protocol

`OntologyClient` / `GuardedQuery` / `ActionExecutor` are typed against this protocol,
not a concrete backend.

```python
insert(obj_type, payload, source) -> str
update(obj_type, obj_id, payload_changes, source) -> None
read_current(obj_type, obj_id) -> StoredObject | None
read_last(obj_type, obj_id) -> StoredObject | None
read_all(obj_type) -> list[StoredObject]
read_page(obj_type, after_key=None, batch=500) -> list[PagedRow]
retire_object(object_type, obj_id) -> StoredObject
erase_object_content(object_type, obj_id) -> EraseResult
object_erasure_state(object_type, obj_id) -> tuple[bool, bool]
create_link(link_type, from_id, to_id) -> None
close_link(link_type, from_id, to_id) -> bool
links_from(link_type, from_id) -> list[str]
links_to(link_type, to_id) -> list[str]
links_from_asof(link_type, from_id, asof) -> list[str]
links_to_asof(link_type, to_id, asof) -> list[str]
append_audit(entry) -> None
audit_entries() -> list[AuditEntry]
transaction() -> ContextManager
capture_action_writes() -> ContextManager[list[WriteRecord]]
```

`read_current` returns the live row only; `read_last` returns the newest row whether
or not it is still live. `links_from`/`links_to` return live links only; the `_asof`
pair also returns links closed at or after the instant you name.

The three history-aware reads exist for ONE caller: the action executor's target gate,
which has to tell "outside your scope" apart from "already retired". A retired object
still owns the scope it was in, and `ActionContext.retire` closes its links, so the gate
resolves that scope from the object's last row plus the links it held when that row
closed. Consumer reads deliberately do not: resolving a retired — or erased — row's
scope would put its children back in a reader's visible set, carrying a population past
`min_n` and releasing an aggregate over the erased subject's own rows. A backend that
filtered retired rows out of `read_last`, or live-only links out of the `_asof` pair,
would deny a retired object's own owner instead.

<!-- scope-asof-invariant:start -->
A retired target resolves the scope it owned **as of the instant its own row closed**.
The links on the chain are read at that instant on both bounds, so an edge the object
had already left cannot answer for it.

Where a `ViaLink` hop finds more than one parent at that instant, the first parent whose
chain resolves wins, over an order the store declares rather than each backend's own row
order: earliest link `valid_from` first, then lowest parent id, compared byte-wise. It
is not creation order — `links` declares no monotonic key, so two links made in one
clock tick are separated by id, not by which was written first. Every backend answers in
that order because it decides which scope owns the object, and therefore which operator
this gate admits.

An ancestor object is admitted when it had not already been retired by then — but it is
read at its newest row, so its payload, and any `DirectProperty` key taken off that
payload, is the one it carries now: an ancestor updated after the instant answers with
the scope it is in today, not the one it was in then. Making the object side
point-in-time too needs an as-of read of an object's history, which `Store` does not
have. A live target has no closing instant and resolves in the present tense, exactly as
a consumer read does, so this gate loosens nothing for an object that still exists.
<!-- scope-asof-invariant:end -->

Point-in-time rather than "retired rows allowed", because the two obvious readings
fail in opposite directions. Resolving ancestors from the newest row lets one
retired long ago outrank a live one and authorize an operator who no longer owns
the row; resolving them live-only refuses an ancestor that was alive when the
target closed and is the only route to a scope that is alive now, denying the
owner permanently. At the target's `valid_to` the earlier-retired ancestor was
already closed and the surviving one was not, so one instant settles both.

<!-- scope-denied-consequences:start -->
Where the chain resolves and the consumer covers it, the gate reaches the handler's own
refusal (`OBJECT_ALREADY_RETIRED`). `SCOPE_DENIED` does not mean one thing: it is raised
at two places. One is a defense-in-depth refusal of a scope-bearing parameter that is
not a `str` — parameter validation rejects that first, so it is a floor under type
confusion rather than a path in normal use. The other fires wherever coverage cannot be
shown, and that is two situations rather than one: the chain resolved and this consumer
is outside it, which is the ordinary denial this gate does not change; or the chain did
not resolve at that instant, and deny-by-default denies. Only the second belongs to this
frame. Four rule kinds are declared, and each meets retirement and erasure at its OWN
hop:

- `SelfScope` answers with the object's own id, which neither retirement nor erasure
  takes away.
- `DirectProperty` reads the scope key off the payload, and `erase_object_content`
  blanks by design what that rule reads, which no history-aware read can recover.
  Retirement leaves the payload alone, because this gate reads the newest row rather
  than the live one, so erasure is the only lifecycle event this hop's OWN READ loses
  to. That is the target itself when the target is `DirectProperty`-scoped; it is
  equally an ANCESTOR whose own hop is `DirectProperty`, which denies a `ViaLink`-scoped
  target whose link erasure preserved. Erasure destroying the scope key is the point of
  erasure, not a gap in the gate.
- `ViaLink` climbs the `links` table, which erasure preserves, so erasure costs this hop
  nothing. It resolves to nothing when none of the parents it reaches resolves in turn —
  among them a parent already retired BEFORE the target closed: its edge may still be
  readable at that instant, but its own row is not admitted, so it cannot answer at the
  one instant this gate asks about. One retired after the target — including in the same
  cascade tick — still answers for it.
- `CustomResolver` is author code handed the raw `Store`, and the engine does not reach
  inside it, so what a retired or erased object resolves to is the resolver's own
  business rather than this frame's. The natural body reads `read_current`, which is
  `None` for a retired object, so a resolver written that way denies. A type that needs
  the precondition refusal after retirement declares a second rule — a `DirectProperty`
  on a scope-key column, which survives retirement.

Each bullet is about one hop, never about one target, and the four are not the whole
chain. `ScopePolicy.rules` maps each type to an ORDERED list, so a target declares as
many of these hops as that list holds and is answered by the first that resolves; and a
level no rule of its own can answer climbs to the canonical instance of a narrower
scope, where a type declares one — which is none of the four. What the engine's own hops
share is the frame: any object the engine has to read that had already been retired
before the target closed is refused there, whichever of those hops reached it — so the
hop that answers a target's level can fail on an object the target's other hops never
touch. A `CustomResolver` is outside that frame only for the reads its own callable
makes: the engine does not thread the instant into author code, so an ancestor the
callable reaches for itself is read however it reads it, retired or not. Its ANSWER
re-enters the engine, and every object the engine reads from there is refused on the
frame's own terms — the canonical instance a narrower answer names, and any object whose
rules the engine goes on to ask, whose row is checked before its own resolver runs.

Every denial in this list fails closed — the object's own owner is denied, nothing is
disclosed.
<!-- scope-denied-consequences:end -->

Three implementations ship, all proven against one 192-assertion conformance suite:

- **`ObjectStore`** — SQLite. History (close-old / insert-new), links, audit log.
- **`InMemoryStore`** — pure Python. No file, no SQL; for tests and dogfooding.
- **`PostgresStore`** — PostgreSQL-backed; available with the `postgres` extra.

The outermost `transaction()` serializes a read followed by a write against
concurrent writers on the same store. It is reentrant, and nested calls share the
outer transaction.

`ObjectStore(registry, path, *, busy_timeout=5.0)` configures how many seconds
SQLite waits for a database lock. `ActionExecutor` holds the outer transaction—and
therefore SQLite's write lock—for the whole handler body, including external
`ctx.capability()` calls. For a competing writer, the time it can wait for that
handler is bounded by its store's busy timeout; expiry raises coded `STORE_BUSY`
(`kind="conflict"`). Increase `busy_timeout` when legitimate handlers can run
longer than the default, or keep capability calls short.

`Source` — `source_system`, `source_id`, `extracted_at`.

`retire_object` closes an object's current row without inserting a replacement;
it does not cascade links. `close_link` closes the one live link matching all
three identifiers. `erase_object_content` is the backend-local operator erasure
primitive: it closes a live object, purges content from object, audit, and
outbox rows, and leaves their structural tombstones in place. All three shipped
backends implement these verbs. The action-context cascade is layered above the
store's object-retirement primitive.

### Schema versioning

Every SQLite file is stamped via `PRAGMA user_version` on create or open.

- A stamp **higher** than the engine's `SCHEMA_VERSION` → `STORE_VERSION_UNSUPPORTED`,
  refused at construction rather than failing later as a confusing SQL error.
- An **unstamped (version-0)** file is inspected, not trusted: every existing table is
  checked against every column the schema requires. A missing pagination cursor column
  is migrated in place (one `ALTER TABLE`, a backfill, an index) and only then stamped.
  Any other missing column → `STORE_SCHEMA_INCOMPATIBLE`, naming table and columns,
  with `user_version` left at 0. A stamp is never written for a file the engine cannot
  actually read.

---

## Bulk ingest

```python
bulk_upsert(store, registry, obj_type, records, source) -> IngestReport
bulk_link(store, registry, link_type, pairs, source) -> IngestReport

client.ingest(
    obj_type, records, source, *,
    on_error: Literal["raise", "report"] = "raise",
) -> IngestReport
client.ingest_links(
    link_api_name, pairs, source, *,
    on_error: Literal["raise", "report"] = "raise",
) -> IngestReport
```

`bulk_upsert` and `bulk_link` are the engine layer and always return an
`IngestReport`. The client methods run the complete batch first, so valid
records remain committed even when another record fails. By default, a failed
client batch raises `IngestError`; its `.report` is the full report and its
message names the committed and failed counts. Pass `on_error="report"` to
return the report without raising, preserving the report-returning behavior.

`IngestReport` — `inserted_ids: list[str]`, `errors: list[IngestError]`. Records are
validated against declared shape: a missing primary key, a missing required property,
an unknown property, or a type mismatch yields `INVALID_RECORD`. Writing an
ontology-owned type raises `OWNED_TYPE_REFUSED`; supplying an ontology-owned property
raises `OWNED_PROPERTY_REFUSED`.

---

## `ontary.connect`

A source-agnostic staging layer between any source system and the ontology, so
swapping a vendor never touches the ontology. 20 engine names.

### Canonical models

- **`CanonicalRecord`** — base class; a `lineage: Lineage` stamp
  (`source_system`, `source_id`, `extracted_at`) is enforced at construction.
- **`CanonicalBatch`** — `entities: dict[str, list[CanonicalRecord]]`.
- **`RawTables`** — source-shaped rows keyed by table name.

### Connectors

- **`SourceConnector`** — the protocol: `extract()` (side-effecting, never called by
  `make verify`) and `transform(raw) -> CanonicalBatch` (pure, unit-tested).
- **`BaseConnector`** — a convenience base.
- **`run_dlt_extract(source, pipeline_name, staging_dir) -> RawTables`** — dlt-backed
  extraction (needs the `dlt` extra).

### Mapping

- **`MappingSpec`** — `object_bindings`, `link_bindings`.
- **`ObjectBinding`** — `entity`, `object_type`, `key_field`, `property_map`,
  `record_model`, `transform`.
- **`LinkBinding`** — `link_type`, `from_entity`, `from_key_field`, `to_entity`,
  `to_key_field`.
- **`map_batch(batch, mapping, registry, store, *, source_system, run_at) -> RunReport`**
- **`run_pipeline(connector, mapping, ontology, store, *, raw=None, run_at=None) -> RunReport`**

`oid(source_system, object_type, key) -> str` derives the deterministic ontology id
that makes mapping idempotent — re-running a batch upserts rather than duplicating.

`RunReport` — `source_system`, `run_at`, `written`, `errors`,
`entities_absent_from_batch`, `links_created`, `links_resolved_same_batch`,
`links_resolved_via_store`, `links_skipped`, `link_skip_details`, `link_errors`.
A batch entity no binding references raises `ENTITY_KEY_MISMATCH`; the reverse (a
bound entity the batch never emits) is counted in the report instead.

### Coercion helpers

`to_date`, `to_datetime`, `to_optional_date`, `to_optional_datetime`.

---

## MCP server

```python
from ontary.mcp_server import build_mcp_server

server = build_mcp_server(ontology, store, consumer, *, name=None,
                          capabilities=None, effects=None)  # -> FastMCP
```

One server process, one `Consumer` identity. Declared handlers arrive **pre-bound** —
there is no registration callback.

Twelve tools, all subject to the same guards as the Python surface. Read-only
tools carry `ToolAnnotations(readOnlyHint=True)`; `execute_action` carries
`ToolAnnotations(destructiveHint=True)`:

| Tool | Purpose | Annotation |
| --- | --- | --- |
| `list_object_types` | Introspection | `readOnlyHint=True` |
| `list_link_types` | Introspection | `readOnlyHint=True` |
| `list_action_types` | Introspection, incl. parameter defs | `readOnlyHint=True` |
| `list_functions` | Introspection | `readOnlyHint=True` |
| `get_declarations` | The declared contract bundle | `readOnlyHint=True` |
| `get_object` | Single read | `readOnlyHint=True` |
| `query_objects` | Filtered/paged read | `readOnlyHint=True` |
| `count_objects` | Visible-row count | `readOnlyHint=True` |
| `aggregate_objects` | Aggregate read | `readOnlyHint=True` |
| `traverse_links` | Follow a link | `readOnlyHint=True` |
| `execute_action` | Run an action | `destructiveHint=True` |
| `call_function` | Call a function | `readOnlyHint=True` |

`query_objects(obj_type, where=None, order_by=None, limit=None, after=None)` is always bounded
on the MCP surface: an omitted `limit` uses the server default cap of 100 rows,
and an explicit limit may be at most 1000. The underlying paged read supplies an
opaque `next_cursor`; a successful response keeps the existing rows under
`result` and adds `next_cursor` alongside it (`null` when exhausted). Pass that
cursor back with the same explicit `limit` to continue. `after` without an
explicit `limit` returns `AFTER_WITHOUT_LIMIT`; values below 1 or above 1000
return `INVALID_LIMIT`.

The `where` grammar accepts a bare scalar for equality or a one-key operator
mapping using `gt`, `gte`, `lt`, `lte`, `in`, `ne`, or `contains`. Operators are
validated against the declared property type; unknown operators raise
`UNKNOWN_OPERATOR`, and an incompatible operator or operand raises
`OPERATOR_TYPE_MISMATCH`. Date comparisons use the stored ISO date order.
Lineage fields are not filterable, and an unknown key raises `UNKNOWN_FIELD`.
`order_by` accepts a declared payload field, ascending by default, or a
`(field, "asc"|"desc")` pair, and composes with the page cursor.
`count_objects` returns the number of rows visible to the consumer and is not
min-N-gated. `aggregate_objects` accepts `func="mean"|"count"|"sum"|"min"|"max"`
with `"mean"` as the default; every function keeps the same min-N release
discipline, including refusal of grouped-empty `{}` selections.
`traverse_links` remains an unpaged list because the underlying
`OntologyClient.traverse`/`GuardedQuery.traverse` API has no `limit`/`after`
cursor surface to delegate to. Pass `reverse=true` to traverse from the link's
target side and return source-side objects; identity-revealing denial is symmetric.

Objects serialize as `{"payload": {...}, "lineage": {...}}` (`None` stays `None`).
Errors return a code from the table below; anything unclassified becomes
`INTERNAL_ERROR` rather than leaking internals to the caller.

Needs the `mcp` extra.

### Multi-consumer serving

```python
from ontary.mcp_server import build_multi_consumer_mcp_server, ConsumerResolver

server = build_multi_consumer_mcp_server(
    ontology, store, *, resolve_consumer, name=None,
    capabilities=None, effects=None,
    token_verifier=None, auth=None,
)  # -> FastMCP
```

One server process, **many proven identities** — no `consumer` argument. Builds exactly
one `OntologyRuntime` (one `GuardedQuery`, one `ActionExecutor`, declared handlers bound
once); every call takes a cheap `runtime.for_consumer(...)` view.

Per invocation: reads that request's MCP-verified `AccessToken` off the transport's own
contextvar, calls the caller-supplied `ConsumerResolver` (`resolve_consumer(token) ->
Consumer | None`), then stamps `Consumer.principal` from the token (`subject`, falling
back to `client_id`) **after** the resolver returns — so a resolver cannot forge who
authenticated. Resolution is never cached: a revoked or re-scoped token can never be
served from a stale binding.

Same twelve tools as `build_mcp_server`, all fail-closed the same three ways, including
introspection:

| Condition | Code |
| --- | --- |
| No verified `AccessToken` on the request (`get_access_token()` returns `None` — no token presented, or HTTP with no `token_verifier` configured; always true over stdio) | `UNAUTHENTICATED` |
| `resolve_consumer` returns `None` for a verified principal | `CONSUMER_UNRESOLVED` |
| `resolve_consumer` raises anything other than a deliberate `OntaryError` | generic `INTERNAL_ERROR` — the raised message never reaches the caller, since it may embed token material |

The SDK verifies no token and issues none — `token_verifier` and `auth` are MCP's own
types (`mcp.server.auth.provider.TokenVerifier` / `mcp.server.auth.settings.
AuthSettings`), configured by the deployer and forwarded VERBATIM to the underlying
`FastMCP(...)` call — the only place either can be wired, since `FastMCP` exposes no
public setter for either afterwards. Passing neither is a legitimate stdio-only or
intentionally-open deployment; passing exactly one of the two is not a runtime state at
all — `FastMCP.__init__` raises `ValueError` (fail-fast at construction), so no server
is ever built and no call is ever made. Stdio carries no auth context at all, so a
multi-consumer server run that way always refuses every call with `UNAUTHENTICATED`
regardless of these two arguments; use `build_mcp_server(ontology, store, consumer)`
for a single-consumer stdio process instead.

**Constructed `stateless_http=True`, hardcoded — a real transport trade-off.**
FastMCP's default stateful streamable HTTP starts one long-lived session task on the
`initialize` request and reuses it for every later request bearing that session's
`Mcp-Session-Id`; a tool body invoked by a later request would then run *inside the
`initialize` request's task*, whose auth context was copied once, at task-start time —
silently breaking "never cached" above, since a revoked or re-scoped token would keep
being served from that frozen binding until the session ended. `stateless_http=True`
gives every request its own fresh transport and session task instead, so there is
nothing to freeze a token into. Cost: no session resumability on this server (SSE
streaming itself is controlled by FastMCP's own `json_response` setting, not by
`stateless_http`, and is unaffected either way). `build_mcp_server` is unaffected — it
has no session to freeze a token into (one `Consumer`, bound at construction, for the
process's whole life).

Needs the `mcp` extra.

---

## Descriptor authoring

The lower-level surface class authoring is built on. Useful for generated or
data-driven ontologies; most authors should use `Ontology`.

| Type | Key fields |
| --- | --- |
| `ObjectTypeDef` | `api_name`, `display_name`, `description`, `layer`, `properties`, `primary_key`, `owned` |
| `PropertyDef` | `name`, `type`, `required`, `sensitivity`, `scope_level` |
| `LinkTypeDef` | `api_name`, `from_type`, `to_type`, `cardinality`, `description`, `identity_revealing`, `owned` |
| `ActionTypeDef` | `api_name`, `display_name`, `target_type`, `executable_by_roles`, `description`, `parameters`, `capabilities`, `effects` |
| `ActionParameterDef` | `name`, `type`, `required`, `refers_to`, `scope_semantics` |
| `FunctionDef` | `api_name`, `description`, `input_description`, `output_description`, `capabilities` |
| `Sensitivity` | `ai_usable`, `human_visible` |

`PropertyType` is `Literal["str", "int", "float", "bool", "datetime", "json"]`.

**`OntologyRegistry`** holds the descriptors and validates cross-references;
`validate()` raises `ValidationFailed` (`ONTOLOGY_INVALID`) on dangling link
endpoints, dangling action targets, duplicate api_names, or a primary key missing
from properties.

**`OntologyDef`** bundles registry + scope policy + config — the unit a client or MCP
server binds to. There is deliberately **no module-global registry anywhere in this
SDK**, so two ontologies coexist in one process with no cross-talk.

**`Declarations`** / `declarations(...)` expose the declared contract as data — what
`get_declarations` serves over MCP: `authority` (model-declared, runtime-checked),
`capabilities` (declared per action/function; fail-closed when unprovided or
undeclared; the provider itself is unsandboxed author code), `effects` (declared per
action; at-least-once via the durable outbox, dispatched after commit), `writeback`
(ontology writes are all ontology-owned; the runtime's own outward path is declared
effects — a declared convention, not an enforced boundary, since a capability provider
can also write outward inline), `reingest` (upsert-merge; owned properties survive; no
deletion), `visibility_default` (deny-by-default — unresolved scope hides),
`transaction_ownership` (runtime-owned — refuses caller-opened transactions),
`ontology_evolution` (fingerprinted; drift is refused unless a declared type version
bump's upcaster chain covers the stored version, or the drift is explicitly accepted),
`idempotency` (none — retries are distinct audited attempts), `audit_scope`
(tenant-scoped administrative view; actions always audited, functions audited iff they
declare capabilities unless overridden per function, and always when a call releases a
hidden field through the contributor exemption), and `tenancy` (one tenant per store instance,
bound at construction). Includes `identity`: on a multi-consumer MCP server, proven by
the transport, never by this runtime — a deployment with no configured verifier
refuses every call rather than assuming a default identity; on a single-consumer
server or in direct Python use, the Consumer is asserted by the operator at
construction and nothing proves it; the verified principal (multi-consumer only) is
mapped to a `Consumer` by a caller-supplied resolver, which is trusted author code the
runtime does not sandbox; both the transport-proved `principal` and the resolved
`actor` are audited, so a resolver that maps every principal onto one privileged actor
is visible in the log; a redelivered effect's audit row restates the actor but not the
principal, joined back by `invocation_id` — so `principal` is `None` on those rows, not
every audited row has one. `min_n` is the one per-ontology answer, read off the
ontology's own `ScopePolicy.min_n`.

---

## Error codes

Every raised error carries a stable code. `OntaryError` is the base;
`ERROR_CODES: dict[str, ErrorCodeInfo]` is the machine-readable registry, and the
table below is generated from it.

### `authority`

| Code | Meaning |
| --- | --- |
| `AUTHORITY_ERROR` | Fallback code for the authority-refusal family (`AuthorityError`); every concrete refusal a captured write can trigger carries its own more specific code instead (e.g. SOURCE_CREATE_REFUSED, UNDECLARED_SOURCE_WRITE). It is the base for `ObjectStore.capture_action_writes` refusals where a write inside an action's capture context crosses the source-backed/ontology-owned line. |
| `OWNED_PROPERTY_REFUSED` | A bulk_upsert record supplied a value for a property declared ontology-owned on an otherwise source-backed object type. |
| `OWNED_TYPE_REFUSED` | A bulk_upsert/bulk_link record targeted an object or link type that is declared whole-type ontology-owned; no source may supply its rows. |
| `SOURCE_CREATE_REFUSED` | A captured `insert` targeted an object type that is not declared whole-type ontology-owned (`ObjectTypeDef.owned is True`). |
| `UNDECLARED_SOURCE_REMOVAL` | A captured retirement or link closure targeted an object or link type that is not declared ontology-owned. |
| `UNDECLARED_SOURCE_WRITE` | A captured `update` touched a property, or a `create_link` targeted a link type, that is not declared ontology-owned. |

### `conflict`

| Code | Meaning |
| --- | --- |
| `CALLER_TRANSACTION_REFUSED` | Raised when `ActionExecutor.execute()` (or an ingest entry point, a later task) is called while the caller has already opened a `store.transaction()` block (declared-contracts §3 AC9). `transaction()` is reentrant, so a caller-owned outer transaction could roll back an action after the executor reported success and audited `ok`. The engine must own the transaction/audit boundary and refuses to nest inside the caller's. Deliberately NOT audited (spec §5): an audit row inside the caller's transaction could itself be rolled back, so the refusal is raised before any audit write. |
| `UPCAST_FAILED` | Raised when a stored row cannot be read as the current declared version. The message distinguishes two causes because their fixes differ: the chain has no step for the carried version (normally a row written by a NEWER ontology than this declaration, i.e. a downgrade, since `ontology.validate()` rejects an incomplete chain), or an author's upcaster raised on this payload. A read failure is deliberately not a silent fallback to the raw payload, which would hand a consumer data in a shape the declaration says does not exist. |
| `ONTOLOGY_DRIFT` | Raised at construction when the declared ontology is not the one this store's rows were written under (ontology-evolution AC3/AC4). It is refused BEFORE any query, for the same reason as the `STORE_VERSION_UNSUPPORTED` conflict: a store the engine cannot honestly serve must not answer a read half-correctly first. Drift is not hypothetical; the measured mild case is a row the typed reader refuses while the string reader returns it. The message names every changed type. The two explicit ways forward are to migrate rows (`ontary.migrate.migrate_object_type`, then `accept_ontology_fingerprint`) or accept drift at the call site with `ObjectStore(..., accept_ontology_drift=True)`, which proceeds and writes an audit entry. |
| `CARDINALITY_VIOLATION` | A link creation would violate its LinkTypeDef cardinality. |
| `OBJECT_ALREADY_ERASED` | An operator erasure targeted an object whose content was already erased; when no newly matched content is found, it returns a coded no-op report rather than raising. |
| `OBJECT_ALREADY_RETIRED` | A retirement targeted an object whose current row is already closed. |
| `STORE_SCHEMA_INCOMPATIBLE` | Raised at `ObjectStore.__init__` when a legacy, never-stamped (`user_version == 0`) file's `objects`/`links`/`audit_log` table ALREADY EXISTS but is missing one or more DDL columns, other than the explicitly migrated `objects.page_token`, `audit_log.effects`, and `audit_log.capability_accesses`. Without this check, `_create_or_migrate_unstamped` would migrate known columns, then `CREATE TABLE IF NOT EXISTS` would silently no-op against a narrower existing table and stamp `SCHEMA_VERSION` anyway: a LYING STAMP. A pre-Milestone-3 file missing `objects.extracted_at` and `audit_log.writes` would then open and every read/write would raise an uncoded `sqlite3.OperationalError` forever because `_init_schema` would not re-inspect a file it believed current. Refusing leaves `user_version` at 0 and the file otherwise untouched for a future engine version with a migration; the message names the table and exact missing columns. |
| `STORE_VERSION_UNSUPPORTED` | Raised at `ObjectStore.__init__` when a store file's `PRAGMA user_version` is HIGHER than this engine's `SCHEMA_VERSION`, or any OTHER non-zero version this engine does not recognize. A newer ontary wrote a schema shape this engine does not know how to read, so construction refuses outright rather than opening and failing later with a confusing SQL error. Version 1 is recognized explicitly and migrated to version 2. The message names BOTH the file's and engine's versions so an operator knows exactly what to upgrade. Never a silent stamp-and-hope: an unreadable version is refused before any other query runs. |
| `STORE_BUSY` | A SQLite transaction could not acquire or retain its database lock within ObjectStore's configured busy timeout; retry after the competing writer finishes or increase busy_timeout. This is a conflict, not a precondition: retrying is the remedy, and the kind travels on the MCP wire so callers can branch on retryability. |

### `internal`

| Code | Meaning |
| --- | --- |
| `INTERNAL_ERROR` | An unclassified failure the MCP surface refuses to describe further, to avoid leaking internals to the caller. |
| `STORE_ERROR` | Fallback code for an unclassified store-layer error. |

### `permission`

| Code | Meaning |
| --- | --- |
| `PERMISSION_DENIED` | The consumer's role is not permitted to execute the action (code `PERMISSION_DENIED`). The permission kind also covers scope refusals under `SCOPE_DENIED`; each raise site supplies the specific code. |
| `SCOPE_DENIED` | The consumer's scope does not cover the action's declared target/scope parameter (code `SCOPE_DENIED`). Role refusals use `PERMISSION_DENIED`; both are kind permission and each raise site supplies the specific code. |
| `UNAUTHENTICATED` | A request carried no verified identity at all -- kind permission. `build_multi_consumer_mcp_server` raises this for every tool call, including introspection, that reaches it with no authenticated `AccessToken`: stdio (which has no auth context) or HTTP with no `token_verifier` configured. The fix is: configure authentication. It is deliberately separate from `CONSUMER_UNRESOLVED`: a missing credential; mapping the principal fixes a missing consumer, so callers can distinguish the two from `.code` alone. |
| `CONSUMER_UNRESOLVED` | A verified principal existed, but the author's `resolve_consumer` callback returned no `Consumer` for it -- kind permission. `build_multi_consumer_mcp_server` raises this when the callback returns `None` for an otherwise verified `AccessToken`; the fix is: map this principal. It remains distinct from `UNAUTHENTICATED`, where no credential was presented at all, so the two failures are distinguishable from `.code` alone. |

### `precondition`

| Code | Meaning |
| --- | --- |
| `CAPABILITY_NOT_PROVIDED` | A declared capability had no provider bound for this call. |
| `EFFECT_NOT_DISPATCHABLE` | A declared effect had no dispatcher bound for this call. |
| `FUNCTION_ERROR` | Registering/calling a Function failed: undeclared api_name, duplicate registration, or no handler bound. |
| `PRECONDITION_FAILED` | An action's precondition failed; the message names it. The conventional code for `ActionError` (kind precondition); an author may attach their own stable code instead (AC7), e.g. `raise ActionError("...", code="GAP_NOT_ACKNOWLEDGED")`. It is also used with overridden codes for unregistered/unhandled actions (`UNKNOWN_ACTION`) and parameter-validation failures (`INVALID_PARAMS`) -- see the `code=` overrides at those raise sites. |

### `validation`

| Code | Meaning |
| --- | --- |
| `AFTER_WITHOUT_LIMIT` | `GuardedQuery.get_objects`'s (or `OntologyClient.list`'s) `after` was given without `limit` (pagination-hardening T2 review P1) -- the unpaginated `Store.read_all` path has no page to resume, so ignoring `after` would let a caller that lost track of its limit silently re-read every visible row and duplicate work; a caller that genuinely wants everything passes no `after` at all. |
| `ENTITY_KEY_MISMATCH` | Raised by `map_batch` when a `CanonicalBatch`'s entity keys and the `MappingSpec`'s `ObjectBinding.entity` names disagree in the authoring-bug direction (spec m35-sdk-refactor AC10). The check is ONE-DIRECTIONAL: batch keys MUST be a subset of binding keys, so a typo such as `widgits` cannot be swallowed by `CanonicalBatch.get()` as zero objects. Binding keys are NOT required to be a subset of batch keys; a partial or incremental connector run may omit normal entities, which is counted in `RunReport.entities_absent_from_batch` instead of failing. |
| `INVALID_BATCH` | `Store.read_page`'s `batch` was < 1 (SQLite's LIMIT -1 means unlimited and InMemoryStore's negative slice drops rows -- both the opposite of a bounded read). |
| `INVALID_CURSOR` | Raised when `Store.read_page`'s `after_key` is malformed OR simply unknown. `after_key` is UNTRUSTED input: it reaches the store from an MCP client via a later page-filling loop, round-tripped from a previous page's cursor without any guarantee the caller did not tamper with it. As amended 2026-07-25 (T2 review, spec §5), it is a random per-row PAGE TOKEN (`objects.page_token`, uuid4 hex), not a decimal row id. Resolving token to row id through the unique index is the ONLY way to turn a cursor into row identity, so every string never issued for a real row (malformed, tampered, or made up) raises this same error on both backends. There is no distinct well-formed but out-of-range case from the old integer design's `OverflowError`/silent-empty-page divergence. A token issued for a row since superseded by `update` still resolves because lookup uses `row_id` independently of `valid_to`, so an in-flight cursor remains a valid resume point (spec §8). |
| `GROUP_KEY_COLLISION` | Two distinct `group_by` values in one selection release as the same dictionary key, so one cell would have to describe two populations. The released shape is `dict[str, ...]` -- a public return type and MCP's wire shape -- and `str()` is not injective over the values a group key can take: an optional property keys `None` on the rows that lack it, which collides with a row carrying the literal string `"None"`. The populations did not merge; the later one overwrote the earlier, so the released value (and, under `func="count"`, the released size) described whichever rows were inserted last, decided by nothing the caller supplied or could observe. Raised per group as each is released, AFTER that group's min-N check, so the release floor keeps precedence over a shape refusal. |
| `INVALID_GROUP_BY` | `GuardedQuery.aggregate_by`'s (or `BoundQuery`'s/`OntologyClient`'s) `group_by` cannot be a group key. Either it was falsy (e.g. "") -- the shared aggregation body branches on `group_by`'s truthiness, so a falsy-but-non-None value would otherwise silently collapse to the ungrouped path and return a float instead of a `dict[str, float]` -- or it names a property whose declared `PropertyType` is not groupable (`json`, whose values may be a `dict` or `list` and so need not be hashable; grouping by one used to raise a bare `TypeError` from inside the grouping loop, and `INTERNAL_ERROR` once it crossed the MCP boundary). The declared type is checked, not the stored values, so a `json` column that happens to hold only scalars refuses too rather than working until the first `dict` arrives. Both are checked in `aggregate_by`, where the `GuardedQuery`, `BoundQuery`, and client surfaces converge, before `_aggregate` runs, rather than relying on an assert removed by `python -O`. |
| `PAGE_NOT_ITERABLE` | A `Page`/`TypedPage` was iterated, indexed or measured directly instead of through `.items`. Both are pydantic models, so the inherited `BaseModel.__iter__` would otherwise yield `(field_name, value)` pairs -- `for row in page` hands back `('items', [...])` and `('next_cursor', ...)`, and the failure surfaces later as `AttributeError: 'tuple' object has no attribute 'payload'` at whatever touched the row. This refuses at the iteration itself and names `.items` and `limit=None`. |
| `INVALID_LIMIT` | `GuardedQuery.get_objects`'s (or `OntologyClient.list`'s) `limit` was < 1 -- a silently empty page would hide that the call was malformed rather than legitimately paginated. |
| `STALE_CURSOR` | An ordered walk's cursor resolved to a row that is no longer current; restart the ordered walk from the first page. |
| `EFFECT_NOT_SERIALIZABLE` | Raised inside the action transaction when an emitted payload cannot be JSON-encoded for the durable outbox (spec `durable-effect-outbox`). This deliberate M5 behavior change means the action rolls back and nothing is sent: before the outbox, an unencodable payload still dispatched while only the audit record degraded to `_safe_json_dumps`'s placeholder, but a durable work item is what a later attempt sends and must not deliver a placeholder as the author's data. Normal payloads use `model_dump(mode="json")` for datetime, UUID, Decimal, enums, and nested models; this takes an arbitrary Python object on a sufficiently loose field. |
| `INVALID_PARAMS` | A call's parameters failed declared-shape validation: an action's params, or a read parameter whose SHAPE is wrong -- an `order_by` that is neither a field name nor a (field, direction) pair, or a `where=` that is not a mapping of field name to condition. A parameter naming something that does not exist is `UNKNOWN_FIELD` instead; this code is about the shape, not the name. |
| `INVALID_RECORD` | A bulk_upsert record failed declared-shape validation (missing primary key, missing required property, unknown property, or a value that does not match its declared type). The validation kind carries the SAME `INVALID_RECORD` code that `bulk_upsert` already reports: from a caller's point of view, a record not matching the declaration is one failure regardless of which write path noticed. This closes the M9 hole where only ingest checked: `Store.insert`/`update` and therefore `ActionContext.insert`/`update` could commit a row missing a required property or carrying a wrong-typed value, report success, and leave the typed reader unable to hydrate it. The same code wraps a Pydantic `ValidationError` while hydrating a stored `OntologyObject` payload (for example, a non-ISO datetime string), never surfacing a bare traceback; a stored row failing declared-shape validation on read-back is the same failure class ingest carries on write. |
| `LINK_NOT_FOUND` | A link closure found no matching live link. |
| `MISSING_MAPPED_FIELD` | A map_batch record's property_map referenced a canonical field entirely absent from that record (not merely None). |
| `NON_NUMERIC_AGGREGATE` | `GuardedQuery.aggregate`'s `value_field` is declared a non-numeric `PropertyType` (anything other than `int`/`float`, such as str/json/datetime/bool). It is checked against the declared type before rows are iterated or coerced, so values that merely look numeric cannot bypass the type contract (spec `m35-sdk-refactor` §6 AC7). |
| `OBJECT_NOT_FOUND` | An update targeted a non-existent object. |
| `OBJECT_ERASURE_NOT_FOUND` | An operator erasure targeted an object with no stored row. |
| `OBJECT_RETIRE_NOT_FOUND` | A retirement targeted an object with no stored row. |
| `ONTOLOGY_INVALID` | `OntologyRegistry.validate()` rejected a declaration because its cross-references were invalid. |
| `SCOPE_POLICY_ERROR` | A ScopePolicy declaration is unusable: a rule references an undeclared object type, link type, or scope level; a type declares an empty contributor rule list; or a type is listed in unscoped_types while also declaring scope rules. |
| `UNDECLARED_CAPABILITY` | A handler requested a capability its action or function did not declare. |
| `UNDECLARED_EFFECT` | An action emitted an effect it did not declare. |
| `UNKNOWN_ACTION` | An action name is unregistered on the OntologyRegistry, or has no handler bound to it. |
| `UNKNOWN_FIELD` | A typed `get`/`list` call named a key that is not one of the target class's declared properties (spec typed-authoring AC7). The existence-only check runs client-side before the guarded read layer; a hidden-but-declared key still reaches the visibility kind unchanged, and the string-form surface keeps its silent-non-match behavior (AC8). The error lives here since C3 of the staged refactor (previously `ontary.functions`, which re-exports it). |
| `UNKNOWN_LINK_TYPE` | An operation referenced an unregistered link type. |
| `UNKNOWN_NAME` | A typed `BoundQuery`/`OntologyClient` call named an unregistered object, link, action, or function -- e.g. an undecorated class, a class/`LinkHandle` registered on a different `Ontology`, or a link api_name absent from this registry (typed-authoring AC7 / typed-actions AC8). Typed lookup failures use the validation kind and live here since C3 of the staged refactor so `ontary._typed_api` can raise them below the runtime modules. |
| `UNKNOWN_OBJECT_TYPE` | An operation referenced an unregistered object type. |
| `UNKNOWN_OPERATOR` | A mapping-form `where` clause named an operator outside the declared set: `gt`, `gte`, `lt`, `lte`, `in`, `ne`, or `contains`. |
| `OPERATOR_TYPE_MISMATCH` | A mapping-form `where` operator is not valid for the property's declared type, or its operand is not a declared-type scalar. |

### `visibility`

| Code | Meaning |
| --- | --- |
| `MIN_N_VIOLATION` | An aggregate would be computed over fewer than min_n distinct contributors. |
| `VISIBILITY_DENIED` | A single-object read/write targeted an object outside the consumer's scope. |

*56 codes across 7 kinds.*

---

## Exception hierarchy

The public kind-class surface is this nine-class hierarchy. `IngestError` is an
additional coded `OntaryError` subclass used for per-record and client-level
ingest failures; its code follows the catalogued failure in the report. Every
error carries a stable `code` from `ERROR_CODES`. Catch a specific kind with
`except <KindClass> as e: e.code`; `except OntaryError as e: e.code` catches every
coded error, including `IngestError`. `MappingValidationError` remains a plain
`Exception` for preflight binding validation and is outside this hierarchy.

| Exception | Parent | Raised when |
| --- | --- | --- |
| `OntaryError` | `Exception` | Root of all coded errors; use it to catch every coded error. |
| `VisibilityError` | `OntaryError` | A visibility rule refuses a read/write or hidden-field operation, or an aggregate has fewer than `min_n` distinct contributors. |
| `PermissionDenied` | `OntaryError` | The caller lacks the required role, scope, or authenticated identity. |
| `PreconditionFailed` | `OntaryError` | A required operation or action precondition is not satisfied. |
| `ValidationFailed` | `OntaryError` | Caller input, declarations, records, or other values fail validation. |
| `AuthorityError` | `OntaryError` | A source or caller attempts a write outside its declared authority. |
| `ConflictError` | `OntaryError` | The requested operation conflicts with store, ontology, schema, or link state. |
| `InternalError` | `OntaryError` | The engine has no more specific coded classification for the failure. |
| `ActionError` | `PreconditionFailed` | An action handler's precondition fails; pass `code="PRECONDITION_FAILED"` or your own stable code. |
| `IngestError` | `OntaryError` | A per-record or client-level ingest failure carrying the report failure's stable code. |
