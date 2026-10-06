# `ontary` — API reference

**English** · [日本語](api-reference.ja.md) · [← README](../README.md)

*Reference* — This page lists the public names of `ontary` for lookup, and [Getting started](getting-started.md) and [Testing your ontology](testing.md) show them in use.

The curated front door of `ontary`: **46 names** in `__all__`. The rest of the
engine remains available from its canonical submodule (`ontary.meta`,
`ontary.store`, and so on).

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
- [Capabilities](#capabilities)
- [Security](#security)
- [Stores](#stores)
- [Bulk ingest](#bulk-ingest)
- [MCP server](#mcp-server)
- [Descriptor authoring](#descriptor-authoring)
- [Error codes](#error-codes)
- [Exception hierarchy](#exception-hierarchy)

---

## Front door

`__all__` is sorted, duplicate-free, importable, and exactly 46 names. These
are the names an ontology author should reach for without choosing an engine
namespace.

### Authoring vocabulary

`ActionContext`, `ActionParams`, `BoundQuery`, `CapabilityHandle`, `Cardinality`,
`Consumer`, `DirectProperty`, `CustomResolver`, `Event`, `FunctionParams`, `LinkHandle`, `Ontology`,
`OntologyObject`, `RowVisibilityStore`, `SelfScope`, `Sensitivity`,
`Source`, `Store`, `ViaLink`, `prop`, `ref`, `scope_ref`, `target`.

### Runtime entries

`Declarations`, `EventRecord`, `Finding`, `InMemoryStore`, `ObjectStore`,
`MCPServer`, `OntologyClient`, `Page`, `PostgresStore`, `ScopePolicy`, `TypedPage`,
`__version__`, `build_mcp_server`, and `declarations`.

`MCPServer` is the `mcp` SDK's server class, re-exported because the MCP
builders return it; it needs the `[mcp]` extra (`pip install 'ontary[mcp]'`).
`import ontary` works without the extra, and only touching `MCPServer` raises
an `ImportError` naming the install command. In a core-only install that
includes `from ontary import *`, which fetches every `__all__` name.

### Error classes

`ActionError`, `AuthorityError`, `ConflictError`, `InternalError`, `OntaryError`,
`PermissionDenied`, `PreconditionFailed`, `ValidationFailed`, and
`VisibilityError`.

The 46-name count is asserted exactly by `tests/test_docs.py`, so a
new root export cannot quietly expand this vocabulary.

```python
from ontary import Ontology, OntologyObject, Consumer, prop, target, Cardinality
```

Python 3.12+. The core package depends only on `pydantic`. Extras: `[mcp]` (MCP
server), `[postgres]` (the `PostgresStore` backend).

---

## Engine surface

Names below are intentionally not flattened into the front door. Import them
from the defining submodule when extending the engine or using an advanced
integration.

### `ontary.actions`

`ActionExecutor`.

### `ontary.audit`

`CapabilityAccessRecord`.

### `ontary.client`

`OntologyRuntime`.

### `ontary.errors`

`ERROR_CODES`, `ErrorCodeInfo`, `Kind`.

### `ontary.functions`

`FunctionHandler`, `FunctionRegistry`.

### `ontary.ingest`

`IngestError`, `IngestReport`, `bulk_link`, `bulk_upsert`.

### `ontary.mcp_server`

`ConsumerResolver`, `build_multi_consumer_mcp_server`.

### `ontary.meta`

`ActionParameterDef`, `ActionTypeDef`, `FunctionDef`, `LinkTypeDef`,
`ObjectTypeDef`, `OntologyRegistry`, `PropertyDef`, `StructFieldDef`, `TransitionDef`, `RuleDef`, `PropertyType`, `ScopeLevel`.

### `ontary.ontology`

`OntologyDef`.

### `ontary.query`

`GuardedQuery`.

### `ontary.scope`

`Direction`, `RowVisibilityFn`, `ScopeRule`, `resolve_contributor`,
`resolve_owning_scope`.

### `ontary.security`

`ConsumerKind`, `covers_scope`.

### `ontary.store`

`AuditEntry`, `DEFAULT_BATCH`, `DEFAULT_TENANT`, `Lineage`,
`SCHEMA_VERSION`, `StoredObject`, `WriteRecord`.

### `ontary.testing`

Public SDK-user test helpers are `make_store`, `consumer`, `raises_code`,
`FixedClock`, `SequentialIds`, `Scenario`, and `scenario`. `make_store(ontology)`
creates a fresh `InMemoryStore`; `consumer(...)` builds a valid `Consumer`; and
`raises_code(code)` asserts a raised error by its machine-readable code — any
`OntaryError`, including `ontary.ingest.IngestError`, as well as structurally
compatible author-defined coded exceptions that expose a stable string `.code`.
`FixedClock(start)` returns
the same timezone-aware datetime on every call and rejects a naive start.
`SequentialIds(prefix)` returns deterministic IDs `prefix-1`, `prefix-2`, and
so on.

`scenario(ontology, *, store=None, clock=None, id_factory=None, capabilities=None)`
returns a `Scenario` for eager, chainable tests. It binds the store, clock, and
id factory once, before any seed write. Defaults are a fresh `InMemoryStore`,
`FixedClock` at `2026-01-01T00:00:00Z`, and `SequentialIds("id")`. Pass an empty
store when overriding. Every method returns the same `Scenario`:

| Method | Meaning |
|---|---|
| `given(*objects)` | Seeds typed `OntologyObject` instances. Allowed only before the first `when`. A store refusal is re-raised as `AssertionError` naming the object and the error code. |
| `given_link(handle, from_, to)` | Seeds a link through a typed `LinkHandle`. Endpoints are objects or id strings. Allowed only before the first `when`. |
| `when(params, *, by)` | Runs one `ActionParams` through the governed runtime as consumer `by` (required). Raises `AssertionError` first if the previous step failed and no `then_error` checked it. |
| `then(cls, pk, **fields)` | Requires the last step to have succeeded. Compares each named property with `==` against the unredacted current row. An undeclared field raises `INVALID_RECORD`. |
| `then_result(expected)` | Requires success and that the return value equals `expected`. |
| `then_error(code)` | Requires failure with `code`, and that current objects and links equal the state before the step. Only this check marks the failure as checked. |
| `then_absent(cls, pk)` | Requires that no current row exists. Works after success or failure. |
| `then_link(handle, from_, to)` | Requires that the link exists in the current state. |
| `then_no_link(handle, from_, to)` | Requires that the link does not exist in the current state. |

Failed checks raise `AssertionError`. Every scenario ends in a `then*` call; a
scenario that ends in a `when` checks nothing.

---

## Authoring an ontology

The `Ontology` constructor, `@ontology.object`, `link`, `prop`, rules, choice and struct properties, field markers, `ActionParams`, events, and advisory findings. Full reference: [Authoring](api-authoring.md).

#### Struct properties and parameters

See [Authoring](api-authoring.md#struct-properties-and-parameters) for struct property and parameter contracts.

## Scope policy

Declared scope rules, `ScopePolicy` fields, and scope resolution helpers. Full reference: [Authoring](api-authoring.md).

## Runtime and clients

```python
runtime = ontology.bind(store, capabilities={...})                  # once
client  = runtime.for_consumer(consumer)                            # cheap, per request
```

### `Ontology.bind(store, *, clock=None, id_factory=None, capabilities=None)`

`clock` is a callable returning a timezone-aware `datetime`; it defaults to
`datetime.now(timezone.utc)`. `id_factory` is a callable returning `str`; it
defaults to UUID-shaped IDs. Both seams are stored on the shared runtime and
are inherited by every `for_consumer()` view.

Binding with `clock=c` installs `c` on the store. From then on every write to
that store uses it: action writes, `ontary.ingest.bulk_upsert` / `bulk_link`,
and direct store calls. `valid_from` / `valid_to` come from it.

- **One instant per invocation.** An action reads the clock exactly once. That
  instant is what `ctx.now()` returns, the `valid_from` / `valid_to` of every
  row and link the action writes (including links `retire` closes), and the
  `ts` of every audit entry of the invocation (ok, denied, error).
- **`CLOCK_CONFLICT`.** Binding the same store with a different clock object
  raises `PreconditionFailed`. The same clock object, or no `clock=`, is fine;
  a runtime bound without `clock=` uses the store's installed clock.
- **`CLOCK_REGRESSION`.** A write whose instant is earlier than the
  `valid_from` of the row or link it closes raises `PreconditionFailed`. Inside
  an action the whole action rolls back and an error audit entry records the
  code. An equal instant is allowed.
- **`CLOCK_NOT_TIMEZONE_AWARE`.** A clock that returns a naive `datetime` raises
  `ValidationFailed`; nothing is stored.

Bind first, then seed: data seeded under another clock can make the first
update or retire under a clock set in the past raise `CLOCK_REGRESSION`.

### `OntologyRuntime(ontology, store, handlers=None, *, clock=None, id_factory=None, capabilities=None)`

Shared, consumer-free machinery for one `(ontology, store)` pair — query layer,
action executor, bound handlers — wired exactly once.

- **`.for_consumer(consumer, *, capabilities=None) -> OntologyClient`** —
  a cheap view. Serving many consumers from one process never re-wires anything.
### `OntologyClient(ontology, store, consumer, *, capabilities=None)`

Bound to exactly one `(ontology, store, consumer)`. Constructing it directly works
and builds a single-use runtime internally.

| Method | Returns |
| --- | --- |
| `.get(obj_type, obj_id)` | `T \| StoredObject \| None` |
| `.list(obj_type, where=None, *, limit=DEFAULT_READ_LIMIT, after=None, order_by=None)` | `list[T] \| list[StoredObject] \| TypedPage[T] \| Page` |
| `.traverse(obj_type, link, from_id, *, reverse=False)` or `.traverse(link_cls, from_obj_or_id, *, reverse=False)` | `list[T] \| list[StoredObject]` |
| `.aggregate(obj_type, value_field=None, where=None, *, func="mean")` | `float \| int` |
| `.aggregate_by(obj_type, value_field, group_by, where=None, *, func="mean")` | `dict[str, float \| int]` |
| `.count(obj_type, where=None)` | `int` |
| `.exists(obj_type, where=None)` | `bool` |
| `.count_contributors(obj_type, where=None)` | `int` |
| `.execute(params)` or `.execute(action, params)` | `dict[str, Any]` |
| `.call_function(params: FunctionParams)` or `.call_function(api_name: str, params: dict[str, Any] \| None = None)` | `Any` |
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
> stored. The store never rewrites what it persists — only the typed client's
> hydration step parses on the way out. What a write accepts is in
> [Date and datetime values](api-stores.md#date-and-datetime-values).

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

`where` accepts a bare scalar for equality or a mapping whose keys are drawn from
`gt`, `gte`, `lt`, `lte`, `in`, `ne`, and `contains`. A mapping with several keys
is their conjunction on that one field, so `{"gte": a, "lt": b}` is a half-open
range and `{"contains": "x", "ne": "x"}` is a substring match with one value
excluded; an empty mapping is refused. Comparisons apply to declared `int`,
`float`, `date`, or `datetime` properties. `date` and `datetime` operands are the
same ISO-8601 strings the property stores, or `date` / offset-aware `datetime`
values, which are converted to that spelling first; a `datetime` must
carry a time component, so a date-only string is `OPERATOR_TYPE_MISMATCH` rather
than a naive midnight that never matches an offset-aware column. The four comparison
operators treat a `datetime` as an instant, so an operand in another UTC offset
matches by moment, not by spelling, and a stored value whose offset-awareness
differs from the operand's (naive against aware) does not match; `eq`, `ne`, and
`in` on a `datetime` still match the stored spelling exactly. `in` takes a list of declared
values, `ne` applies to every declared type, and `contains` is a substring test
for `str`. Unknown operators raise `UNKNOWN_OPERATOR`; an operator or operand
that does not match the declared property type raises
`OPERATOR_TYPE_MISMATCH` — including the bare scalar, which is the only equality
spelling. `None` is the exception: it is the null test, selecting rows where the
property is absent. An `int` operand on a declared `float` is widening, not a
mismatch. Mapping-valued `where` is the same grammar on typed,
string, aggregate, Function, and MCP read surfaces.

A `where` condition on a struct property raises `OPERATOR_TYPE_MISMATCH` with a
message such as `where is not supported on struct property 'amount'`. Inner paths
such as `amount.currency` cannot be queried; they are not top-level properties.

Because a mapping value is operator syntax, dict equality on a declared `json`
property is not spelled `where={"data": {"kind": "a"}}` — that mapping is parsed
as operators, not a value to match. Wrap it as an `in` list of one instead:
`where={"data": {"in": [{"kind": "a"}]}}` is the equality escape.

For a hidden property declared as a `DirectProperty` scope-routing key, the
scope-key exemption applies only to a bare `eq` value and a lone `in` over an
explicit list. `gt`, `gte`, `lt`, `lte`, `ne`, and `contains`, plus `in` over a
non-list, any malformed shape, and any multi-key mapping (even one that contains
an `in`), are refused with `VISIBILITY_DENIED` because they let the caller learn
a value. The operand must also actually supply a value — a `str`,
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
  A malformed or unknown cursor → `INVALID_CURSOR`. An ordered walk cannot resume
  after any write to its own cursor row — including a retirement or an `update`
  that supersedes it — and raises `STALE_CURSOR`; restart from the first page.

**Scale caveat for ordered pages.** Each ordered page currently materializes and
sorts the whole object type, regardless of `limit` or `where` selectivity. In the
measured 20,000-row case, `limit=10` read all 20,000 rows with `order_by` versus
500 without it: O(N log N) per ordered page, and O(N² log N) for a full ordered
walk.

### `traverse`

`client.traverse(link_cls, from_obj_or_id)` is the typed form;
`client.traverse("Comment", "commentOnTicket", comment_id)` names the source type,
link API name, and source id in that order. `BoundQuery` accepts the same
handle-first typed form inside Functions.

Returned targets are subject to the same visibility checks as any other read.
Traversal through an identity-revealing link raises `VisibilityError` with
`VISIBILITY_DENIED` for a human consumer before the target is returned; AI
consumers still receive normal scope and sensitivity enforcement on the target
rows. `reverse=True` traverses from the link's target side, and the
identity-revealing denial is symmetric in both directions.

### Visible-row counting

`count(obj_type, where=None)` returns the number of matching rows after the
consumer's scope and row-visibility checks; `exists(...)` reports whether that same
post-visibility selection is non-empty. Both accept the same `where` operator
grammar as `list`. A consumer scoped away from every matching row receives `0`
from `count` and `False` from `exists`, not a privacy refusal.

These two operations are deliberately not min-N-gated. They reveal only the size
of a row set after post-visibility filtering, and the consumer
can already enumerate the same rows with `list(..., limit=None)`, so a min-N
refusal would add no disclosure protection.
`count_contributors` remains the sole privacy-counting primitive: unlike
visible-row counting, it resolves the distinct contributor population behind an
aggregate and therefore keeps the aggregate's min-N release discipline.

### Aggregates

`aggregate(...)` and `aggregate_by(..., group_by=...)` accept
`func="mean"|"count"|"sum"|"min"|"max"`; the default remains `"mean"`.
Ungrouped aggregation returns a plain numeric value: `count` is an `int` and
the other functions return `float`. Grouped aggregation returns the corresponding
value per group in a `dict`.

`func="count"` accepts any declared field type — it counts the rows carrying
that field and never coerces the values to `float`. Its `value_field` may also
be omitted (or passed as `None`) to count every visible row in the selection,
min-N gated over those rows' contributors (per group, when grouped); omitting
`value_field` for any other func raises `INVALID_PARAMS`, naming the func and
saying `value_field` is required.

Both enforce hidden-field checks on `where`/`group_by`, min-N over **distinct
contributors**, `UNKNOWN_FIELD` for a `value_field` the type does not declare, and
`NON_NUMERIC_AGGREGATE` for a declared but non-numeric one under `mean`/`sum`/`min`/`max`
(`count` is exempt; both checks run before any row is read). A hidden `value_field` may be aggregated only from an
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
Struct properties also raise `INVALID_GROUP_BY` when used as `group_by`; the
declared struct value is not a supported group key.

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
`@ontology.action(params_cls, target=..., roles=[...], capabilities=())`.

Every `execute` runs the same pipeline:

**registered?** → **role permitted?** → **scope covers the declared target/scope
param?** → **preconditions** → **transactional side effects** → **append-only audit**

Every attempt is audited — `ok`, `denied`, and `error` alike.

### `ActionContext`

What a handler receives instead of a raw store handle. Writes are auto-stamped with
the action's own `Source`.

**Typed members.** These take the ontology's own classes and `LinkHandle`s, so
`mypy` checks the class, the returned type, each link endpoint's type, and every
attribute a handler assigns before `save`. `create`'s keyword names are checked at
runtime only.

| Member | Purpose |
| --- | --- |
| `.get(cls, obj_id) -> T \| None` | Read one current object |
| `.all(cls) -> list[T]` | Every current object of `cls` |
| `.create(cls, **values) -> T` | Create and return the object. A missing primary key is minted from the runtime's `id_factory`; a primary key that is already live is refused with `OBJECT_ALREADY_EXISTS`; a name that is not a declared property is refused with `INVALID_RECORD` |
| `.save(obj)` | Write the declared properties changed since this context handed `obj` out, and nothing when none changed. Only an object from `get`, `all`, `create` or `traverse` can be saved (`OBJECT_NOT_LOADED` otherwise); a changed primary key is refused with `PRIMARY_KEY_IMMUTABLE` |
| `.link(handle, from_, to)` | Link. Each end is an object or its id, typed by the handle. Both ends must be live objects of the link's declared endpoint types, or the call is refused with `LINK_ENDPOINT_NOT_FOUND`. A link identical to a live one is a no-op |
| `.unlink(handle, from_, to)` | Close one live link, ends as for `link` |
| `.traverse(handle, anchor) -> list[To]` | The `To` objects linked from `anchor`; `reverse=True` returns the `From` objects linked to it |
| `.retire(obj)` / `.retire(cls, obj_id)` | Retire the object and close every live link that touches it |

A `date` or `datetime` value passed to `create` or assigned before `save` follows
[Date and datetime values](api-stores.md#date-and-datetime-values); a refused value raises
`INVALID_RECORD`.

A typed handler reads an object, changes it, and saves it:

```python
order = ctx.get(Order, params.order_id)
order.status = "shipped"          # mypy checks the field name and type
ctx.save(order)                   # writes only `status`
```

**Removed in 0.18.0.** Use the typed replacements below. The string form of
`retire` or `unlink` raises `ValidationFailed` with code `INVALID_PARAMS`.
`unlink` is positional-only. A keyword call raises `TypeError`.

| Removed call | Typed replacement |
| --- | --- |
| `.insert(obj_type, payload)` | `.create(cls, **values)` |
| `.update(obj_type, obj_id, changes)` | `.get(cls, obj_id)` + assignment + `.save(obj)` |
| `.create_link(link_api_name, from_id, to_id)` | `.link(handle, from_, to)` |
| `.retire(obj_type, obj_id)` | `.retire(obj)` / `.retire(cls, obj_id)` |
| `.unlink(link_api_name, from_id, to_id)` | `.unlink(handle, from_, to)` |
| `.read_current(obj_type, obj_id)` | `.get(cls, obj_id)` |
| `.read_all(obj_type)` | `.all(cls)` |
| `.links_from(link_api_name, from_id)` | `.traverse(handle, anchor)` |
| `.links_to(link_api_name, to_id)` | `.traverse(handle, anchor, reverse=True)` |

**Other members.**

| Member | Purpose |
| --- | --- |
| `.capability(handle) -> P` | Fetch a declared capability |
| `.consumer` | The calling `Consumer` |
| `.emit(event, *, about=None)` | Record a declared event on this invocation. See Events under Authoring an ontology |
| `.now() -> datetime` | The invocation's single instant (see `Ontology.bind`). Use it instead of `datetime.now()` or a clock capability |

`get` and `all` are trusted handler reads. They return raw, unredacted, and
unscoped objects. They are intentionally not guarded consumer queries. Filtering
an enumeration by scope or sensitivity could hide an existing row from an id
allocator and cause id reuse.

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
`invocation_id`, `error_code`, plus integrity records: `writes: list[WriteRecord]`,
`capability_accesses: list[CapabilityAccessRecord]`, `events: list[EmittedEvent]`.

**`kind: Literal["action", "function"]`** — what produced the entry. Actions and
functions share one log; `kind` is how a reader tells them apart, since nothing stops
an ontology declaring an action and a function with the same `api_name`. On a
`function` entry, `action` holds the function's api_name, `target_type` is `""` (a
function has no target object type), and `writes` is always empty.

**`invocation_id: str | None`** — one id per `execute()` (or audited
`call_function()`) call, stamped on *every* entry that call writes. Correlate
entries by this value rather than by matching fields and append order — two calls to
the same action with the same params are otherwise indistinguishable. `None` means
the entry predates the field (a store file written by an older engine); it is never
invented for such rows.

**`events: list[EmittedEvent]`** — the events an `ok` action entry emitted, in emission
order and unredacted, since audit is the administrative view. Every `denied` and
`error` entry lists none.

**`unscoped_params: list[str]`** — the `target(...)` parameters that skipped the
action scope gate because they refer to a `scope="unscoped"` type. For them the
action's `roles=` was the only gate. The list is empty when every scope-bearing
parameter was scope-checked, and on entries written before the gate ran (for
example a role denial).

**`target_id: str | None`** — on an `action` entry, the id the caller passed in the
action's declared target parameter. Every outcome records it, including `denied` and
`error`, even when that id does not exist. `None` when the action declares no target
parameter, and on every `function` entry.

**`error_code: str | None`** — why a `denied` or `error` entry failed: the raised
exception's catalogued `code` (for example `PERMISSION_DENIED`, `SCOPE_DENIED`,
`INVALID_PARAMS`, or a handler's own `ActionError` code). An exception that carries
no code, such as a bare `KeyError` from a handler, is recorded as `INTERNAL_ERROR`,
the code the MCP server reports for it. `None` on an `ok` entry.

- `WriteRecord` — `op` (`create`/`update`/`link`), `object_type`, `link_type`,
  `object_id`, `from_id`, `to_id`
- `CapabilityAccessRecord` — `api_name`, `count`
- `EmittedEvent` — `event_type`, `about_type`, `about_id`, `payload` (storage form)

---

## Functions

A function handler takes a `BoundQuery`. A typed function also declares a
`FunctionParams` subclass with `@ontology.function(params_cls, ...)`.
`FunctionParams` rejects unknown fields; its annotations define the validated
inputs and MCP parameter schema. No store handle ever reaches the handler — only
already-guarded reads. Functions return derived values and never write.

```python
class TicketStatsParams(FunctionParams):
    queue_id: str

@ontology.function(TicketStatsParams, api_name="ticketStats")
def ticket_stats(query: BoundQuery, params: TicketStatsParams) -> float:
    mean = query.aggregate("Ticket", "age_hours", where={"queue_id": params.queue_id})
    assert isinstance(mean, float)
    return mean

client.call_function(TicketStatsParams(queue_id="q1"))
```

There are two declaration forms. A typed function declares a `FunctionParams`
subclass; callers may pass an instance as above or pass its values in a dict with
the function name, such as `client.call_function("ticketStats", {"queue_id": "q1"})`.
The dict is validated and the handler receives a `TicketStatsParams` instance.
A no-input function omits the params class and takes only `query`:

```python
@ontology.function(api_name="health")
def health(query: BoundQuery) -> bool:
    return query.exists("Ticket")

client.call_function("health")
client.call_function("health", {})
```

Without a params class, the handler must take exactly one parameter, `query`, with no
default. Any other handler is refused at declaration with `ValidationFailed` and
`code="ONTOLOGY_INVALID"`. Dict-form handlers such as `(query, params: dict)` were
removed in 0.20.0; declare a `FunctionParams` subclass instead.

Unknown fields, missing required fields, invalid types, and values outside
declared choices raise `ValidationFailed` with `code="INVALID_PARAMS"` before the
handler runs. A no-input function also rejects non-empty params with that code.

`FunctionDef.parameters` and MCP `list_functions` expose typed inputs as
parameters with `name`, `type`, `choices`, `fields`, `required`, and `refers_to`,
matching the action parameter shape without `scope_semantics`. The value is
`[]` for a no-input function.
`client.call_function` refuses an argument that is neither a function name nor a
`FunctionParams` instance with `ValidationFailed` and `code="INVALID_PARAMS"`.

### `BoundQuery`

A `GuardedQuery` with the consumer fixed: `.get`, `.list`, `.count`, `.exists`, `.traverse`,
`.aggregate`, `.aggregate_by`, `.count_contributors`,
`.capability(handle)`, `.now()`. Typed overloads
work the same as on `OntologyClient` (`query.get(Ticket, id) -> Ticket | None`).

`query.now()` returns the call's single instant from the runtime's bound clock,
the same clock as `ctx.now()`. The clock is read on first use, and later calls
in the same Function call return that instant. Use it in a time-dependent
Function instead of `datetime.now()` or a clock capability. A declared
capability makes every call audited.

```python
@ontology.function(api_name="overdueIds")
def overdue_ids(query: BoundQuery) -> list[str]:
    now = query.now()
    return [order.id for order in query.list(WorkOrder) if order.due < now]
```

For a function with no inputs, declare a one-argument handler `(query)` and call
it with no params or `{}`.

`PreconditionFailed` (`FUNCTION_ERROR`) covers an undeclared api_name, a duplicate
registration, or no bound handler.

### The function audit boundary

For an audited call, `OntologyClient.call_function` appends one `kind="function"` audit entry —
carrying the invocation id, the params, the outcome, and the handler's
`capability_accesses`. `writes` is empty by construction.

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
query layer's bound includes an exemption for author-declared functions on types
with contributor rules: a capability-less function can hand a consumer a `mean`
or `count` over a field they cannot read themselves, for the complete visible
population only. When a call actually does that, `call_function` appends its entry —
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

## Capabilities

Anything a handler wants from the outside world must be **declared**, then provided
at bind time. Undeclared use is refused, and every use is audited. A capability is
a thing a handler reads or calls.

```python
Mailer = ontology.capability(MailerProto, name="mailer")

@ontology.action(P, target=T, roles=["Agent"], capabilities=[Mailer])
def handler(ctx, params):
    ctx.capability(Mailer).send(...)
```

For the current time use `ctx.now()` in an action and `query.now()` in a Function, not a capability.

Requesting an undeclared capability raises `UNDECLARED_CAPABILITY`; a declared one
with no provider bound raises `CAPABILITY_NOT_PROVIDED`.

Providers are bound at `ontology.bind(store, capabilities={...})` or per client via
`for_consumer(...)`.

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

The `Store` protocol, the SQLite and Postgres backends, date/datetime value rules, and schema versioning. Full reference: [Stores & ingest](api-stores.md).

### Date and datetime values

See [Stores & ingest](api-stores.md#date-and-datetime-values) for the value write rules.

## Bulk ingest

`bulk_upsert`, `bulk_link`, client ingest methods, validation, and ingest reports. Full reference: [Stores & ingest](api-stores.md).

## MCP server

```python
from ontary.mcp_server import build_mcp_server

server = build_mcp_server(ontology, store, consumer, *, name=None,
                          capabilities=None)  # -> MCPServer
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

`list_object_types` includes a `transitions` key on every property. Its value is
`null` when the property has no graph, or an object with the complete `initial`
state list and `moves` mapping when it does. Each object type also has a `rules`
list containing each rule's `name` and `message`; rule code is never included.

`query_objects(obj_type, where=None, order_by=None, limit=None, after=None)` is always bounded
on the MCP surface: an omitted `limit` uses the server default cap of 100 rows,
and an explicit limit may be at most 1000. The underlying paged read supplies an
opaque `next_cursor`; a successful response keeps the existing rows under
`result` and adds `next_cursor` alongside it (`null` when exhausted). Pass that
cursor back with the same explicit `limit` to continue. `after` without an
explicit `limit` returns `AFTER_WITHOUT_LIMIT`; values below 1 or above 1000
return `INVALID_LIMIT`.

The `where` grammar accepts a bare scalar for equality or an operator mapping
using `gt`, `gte`, `lt`, `lte`, `in`, `ne`, or `contains`; several operators in
one mapping are AND-ed, so `{"gte": a, "lt": b}` is a range. Operators are
validated against the declared property type; unknown operators raise
`UNKNOWN_OPERATOR`, and an incompatible operator or operand raises
`OPERATOR_TYPE_MISMATCH`. Date comparisons use the stored ISO date order;
datetime comparisons are by instant across UTC offsets.
Lineage fields are not filterable, and an unknown key raises `UNKNOWN_FIELD`.
`order_by` accepts a declared payload field, ascending by default, or a
`(field, "asc"|"desc")` pair, and composes with the page cursor.
`count_objects` returns the number of rows visible to the consumer and is not
min-N-gated. It reveals only what `query_objects` already lists; for a min-N-released
count use `aggregate_objects(func="count")`, which needs no `value_field`.
`aggregate_objects` accepts `func="mean"|"count"|"sum"|"min"|"max"`
with `"mean"` as the default; every function keeps the same min-N release
discipline, including refusal of grouped-empty `{}` selections. `value_field` is
optional for `func="count"` — omitting it counts every visible row; every other
func requires it and raises `INVALID_PARAMS` if it is missing.
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
    capabilities=None,
    token_verifier=None, auth=None,
)  # -> MCPServer
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
`MCPServer(...)` call — the only place either can be wired, since `MCPServer` exposes no
public setter for either afterwards. Passing neither is a legitimate stdio-only or
intentionally-open deployment; passing exactly one of the two is not a runtime state at
all — `MCPServer.__init__` raises `ValueError` (fail-fast at construction), so no server
is ever built and no call is ever made. Stdio carries no auth context at all, so a
multi-consumer server run that way always refuses every call with `UNAUTHENTICATED`
regardless of these two arguments; use `build_mcp_server(ontology, store, consumer)`
for a single-consumer stdio process instead.

**Transport options belong to `run()`/`streamable_http_app()`.** The builder does not
force a session mode: on mcp 2.x, `stateless_http`, `json_response`,
`transport_security`, and `host` are keyword arguments of `run()` and
`streamable_http_app()`, and `port` of `run()` (an ASGI app binds no socket). Each request resolves its own token in stateful
sessions too; `tests/test_mcp_multi_consumer.py` pins both modes at the ASGI
boundary. `build_mcp_server` has one `Consumer` bound at construction and no
per-request identity to resolve.

Needs the `mcp` extra.

---

## Descriptor authoring

Descriptor-based declaration, `Declarations` / `declarations(...)`, and legacy handlers. Full reference: [Authoring](api-authoring.md).

## Error codes

The stable `ERROR_CODES` registry, meanings grouped by seven kinds, and the code-count summary. Full reference: [Error codes](api-errors.md).

## Exception hierarchy

The public exception classes, their parents and raise conditions, and catching errors by stable code. Full reference: [Error codes](api-errors.md).
