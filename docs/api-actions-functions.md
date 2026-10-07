# `ontary` — API reference: Actions & Functions

**English** · [日本語](api-actions-functions.ja.md) · [← API reference](api-reference.md)

*Reference* — Action and Function contracts; see the [API reference](api-reference.md) and [Getting started](getting-started.md) for related contracts and examples.

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
parameters with `name`, `type`, `description`, `choices`, `fields`, `required`, and
`refers_to`,
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

