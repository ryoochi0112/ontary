# `ontary` — API reference: Runtime & clients

**English** · [日本語](api-runtime.ja.md) · [← API reference](api-reference.md)

*Reference* — Runtime & clients contracts; see the [API reference](api-reference.md) and [Getting started](getting-started.md) for related contracts and examples.

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
> its name in `redacted_fields`. On the **string surface the key is absent from
> `payload` entirely**, and there is no `redacted_fields` companion. Over **MCP the key
> is also absent from `payload`**, and each row carries a `redacted_fields` list of the
> hidden names (see [MCP read results](api-mcp.md#read-results)). Read defensively on the
> string and MCP surfaces (`payload.get("email")`, not `payload["email"]`), and do not
> infer "not stored" from a missing key: it may simply be hidden from you.

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
   On the typed surface, a hidden field reads back `None` and is named in
   `redacted_fields`. On the string surface the key is absent from `payload`. Over MCP
   the key is absent and the row's `redacted_fields` names it.
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

