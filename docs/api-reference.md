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

`StoredObject`, `Lineage`, filters, ordering, bounded reads, `Page`/`TypedPage[T]` pagination, `traverse`, visible-row counting, aggregates, and `GuardedQuery`. Full reference: [Reading](api-reading.md).

## Actions

An action combines typed parameters with a handler declared by `@ontology.action`; `ActionContext` provides handler operations, authority governs writes and removals, and the `AuditEntry` record captures each attempt. Full reference: [Actions & Functions](api-actions-functions.md).

### `ActionContext`

See [Actions & Functions](api-actions-functions.md#actioncontext) for the handler context contract.

## Functions

`@ontology.function` declares derived computations using guarded reads through `BoundQuery`, including `query.now()`; the function audit boundary determines which calls are recorded. Full reference: [Actions & Functions](api-actions-functions.md).

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
