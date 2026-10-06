# `ontary` — API reference: Authoring

**English** · [日本語](api-authoring.ja.md) · [← API reference](api-reference.md)

*Reference* — Ontology authoring and declaration contracts; see the [API reference](api-reference.md) and [Getting started](getting-started.md) for related contracts and examples.

## Authoring an ontology

### `Ontology(name, scope_levels, min_n=3)`

The authoring facade. Accumulates an `OntologyRegistry`, a `ScopePolicy`, and the
declared handlers, then hands out runtimes.

| Parameter | Type | Notes |
| --- | --- | --- |
| `name` | `str` | Also the default MCP server name. |
| `scope_levels` | `list[str]` | Your own hierarchy, coarsest last — e.g. `["queue", "org"]`. There are no built-in levels. |
| `min_n` | `int` = `3` | Minimum distinct contributors for any aggregate. |

An empty or duplicated `scope_levels`, or `min_n` below 1, is refused with
`ValidationFailed` (`ONTOLOGY_INVALID`) when `.definition` is first built.

Declaration methods, all decorators except `link`:

| Method | Purpose |
| --- | --- |
| `@ontology.object(...)` | Register an `OntologyObject` subclass as an object type |
| `ontology.link(api_name, from_cls, to_cls, cardinality, ...)` | Register a link type; returns a `LinkHandle` |
| `@ontology.action(params_cls, ...)` | Register a typed action handler |
| `@ontology.function(...)` | Register a derived-value function |
| `ontology.capability(proto, ...)` | Declare a capability; returns a `CapabilityHandle` |
| `ontology.validate(store=None)` | Validate and **freeze** registration; with a store, also refuse stored rows that no longer hydrate |
| `ontology.diagnose(store=None)` | Return every `Finding` without raising or freezing; with a store, also sweep its rows |
| `ontology.bind(store, ...)` | Build an `OntologyRuntime` |

`validate()` (and touching `.definition`) freezes the ontology — any later
`object`/`link`/`action`/`function` call raises. Call it once, after every
declaration.

**Checks that teach.** Two modelling mistakes that used to pass `validate()` and
fail later are now caught where they are written:

- An action whose `target(...)` parameters all refer to another type than its
  `target=` is refused at `@ontology.action(...)` with `ONTOLOGY_INVALID`, naming
  the action, the parameters, and both types. Before, the scope gate checked the
  parameter's object while the audit entry and MCP named `target=`. One `target()`
  parameter must refer to the `target=` type; extra `target()` parameters of other
  types are allowed. `validate()` and `diagnose()` apply the same rule to a
  hand-built `OntologyRegistry`.
- Rows written before an ontology edit (a property made required, a changed type,
  narrowed `choices`) fail only when first read, because a store records no
  fingerprint of the ontology. `diagnose(store=store)` reads every current row
  with `Store.read_all` and returns one `INVALID_RECORD` finding per type and
  property, with the number of rows that would fail hydration.
  `validate(store=store)` raises `INVALID_RECORD` for them. The sweep reads every
  row, so it runs only when you pass a store, never at `bind()`.

```python
ontology.validate(store=store)  # before serving an edited ontology
```

#### `@ontology.object(*, layer, owned=False, api_name=None, description=None, display_name=None, scope: Literal["unscoped"] | Sequence[ScopeRule] | None = None, contributor: Sequence[ScopeRule] | None = None, row_visibility: RowVisibilityFn | None = None)`

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
- **`accept`** — a lint code or a sequence of codes (`FORBIDDEN_TYPE_NAME`,
  `AUDIT_TYPE`) this type accepts. **`snapshot`** — `True` declares a snapshot type.
  Both are described in [Advisory findings](#advisory-findings-findingguide-accept-and-snapshot).

`scope`, `contributor`, and `row_visibility` are shape-checked when the class is
decorated. A misspelled `"unscoped"`, a single rule not wrapped in a list, or a
non-callable `row_visibility` raises `ValidationFailed` (`ONTOLOGY_INVALID`) right
there, naming the class, the kwarg, what was given, and the accepted forms. The
`Literal` annotation makes the misspelled string a mypy error as well.

#### `ontology.link(api_name, from_cls, to_cls, cardinality: Cardinality | Literal["ONE_TO_ONE", "ONE_TO_MANY", "MANY_TO_ONE", "MANY_TO_MANY"], *, description=None, identity_revealing=False, owned=False) -> LinkHandle`

`cardinality` is a `Cardinality` enum member or its string name:
`ONE_TO_ONE`, `ONE_TO_MANY`, `MANY_TO_ONE`, `MANY_TO_MANY`. Any other string is
refused with `ValidationFailed` (`ONTOLOGY_INVALID`) listing those four names, and
the `Literal` annotation makes the typo a mypy error as well.

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

#### `prop(*, primary_key=False, sensitivity=None, scope_level=None, required=None, property_type=None, choices=None, transitions=None, **field_kwargs)`

`pydantic.Field(...)` plus ontology metadata. Unrecognized kwargs pass straight
through to `Field`, so `prop(default=None, description="...")` behaves as expected.
`prop()` is never a parallel field system: bare annotated fields and plain `Field(...)`
keep working on the same class.

> **A property with restricted `sensitivity` must be declared `X | None`.** The
> decorator raises a coded validation error at class-registration time otherwise —
> because a redacted read has to be able to return `None` for it.

`choices=["open", "closed"]` limits a `str` property to those values. Every write
path refuses any other value.

`accept="STORED_DERIVABLE"` (or `"FREE_TEXT_STATUS"`, or a sequence of them) accepts
that advisory finding for this property. See
[Advisory findings](#advisory-findings-findingguide-accept-and-snapshot).

`transitions=TransitionDef(initial=(...), moves={...})` declares allowed moves on
a choice property. String values and string-valued `Enum` members (plain `Enum`
or `StrEnum`) are accepted as states. A primary key cannot have transitions.

Use `prop(transitions=...)` to attach the graph, and import `TransitionDef` from
`ontary.meta`; it is not exported from `ontary.__all__`.

A write that breaks the graph is refused with `TRANSITION_NOT_ALLOWED`. The
message names the current state, the requested state, and the allowed moves:

```text
TRANSITION_NOT_ALLOWED: Order 'o-1': status cannot move from 'pending' to 'shipped'; allowed from 'pending': ['paid']
```

The line shows the error code, then its message. The message, `str(exc)`,
does not include the code; read the code from `exc.code`.

#### `Ontology.rule(cls, name, *, message)`

Decorate a typed predicate after registering `cls` with `@ontology.object`:

```python
@ontology.rule(Order, "shipped_needs_payment", message="payment required")
def shipped_needs_payment(order: Order) -> bool:
    return order.status != OrderStatus.SHIPPED or order.paid_at is not None
```

The predicate receives a hydrated object built from the full stored row and
must read only that object. Its name and message appear in the type declaration;
the callable is omitted from exported schema data. Rules can be registered
until `ontology.definition` freezes the ontology.

A write whose new row fails a rule is refused with `RULE_VIOLATED`. The message
names the rule and its message:

```text
RULE_VIOLATED: Order 'o-1': rule 'shipped_needs_payment': payment required
```

An action assigns the new status and saves. It does not pre-check the declared
moves or rules: the engine refuses the write, and tests should expect the
engine's code.

#### Choice properties: `Enum` and `Literal`

Annotate a property with a string-valued `Enum` (for example a `StrEnum`) or a
`Literal[...]` of strings. It declares the same shape as `str` plus `choices`: the
`PropertyDef` has `type="str"`, and its `choices` are the member values in
declaration order.

```python
class Status(StrEnum):
    OPEN = "open"
    CLOSED = "closed"


@ontology.object(layer="L0", scope="unscoped")
class Ticket(OntologyObject):
    id: str = prop(primary_key=True)
    status: Status
    kind: Literal["bug", "task"]
```

- **Storage.** The store keeps the member's string value. Every write path accepts
  an `Enum` member or its value: `ingest`, `ctx.create`, `ctx.save`, action
  parameters, and `where` filters. Any other value is refused (`INVALID_RECORD` on
  a write, `INVALID_PARAMS` on an action). A `where` filter does not refuse a value
  outside the choices; it matches no rows.
- **Only where `choices` are declared.** An `Enum` member is unwrapped to its value
  by the declaration, not by its Python type. A `prop(choices=...)` property accepts
  a member whose value is one of its choices, because it is the same declaration. A
  plain `str` property without `choices` still refuses an `Enum` member
  (`INVALID_RECORD` on a write, `OPERATOR_TYPE_MISMATCH` in a `where` filter), as
  it did before.
- **Reads.** A typed read returns the `Enum` member, or the `Literal` string, so
  `mypy` narrows the field. A dict read, an MCP result, and an `aggregate_by` key
  return the plain string.
- **Action parameters.** The same annotations on an `ActionParams` field set
  `choices` on its `ActionParameterDef`, and the typed handler receives the member.
  `prop(choices=[...])` on a `str` parameter sets `choices` too, as on a property,
  and the handler receives the string.
- **MCP.** `list_object_types` and `list_action_types` list the allowed values
  under `choices` (`null` when a property or parameter has none).
- **Refused at declaration** (`ONTOLOGY_INVALID`): a member that is not a string,
  such as an `IntEnum` or `Literal[1, 2]`; a choice annotation combined with
  `prop(choices=...)`; and a choice annotation on the primary key.

#### Struct properties and parameters

Annotate a property or an `ActionParams` field with a flat Pydantic `BaseModel`.
For example, `amount: Money` declares a struct property. The same annotation on an
action parameter declares a struct parameter.

```python
class Money(BaseModel):
    value: float
    currency: str


class Order(OntologyObject):
    amount: Money
```

The generated `PropertyDef` or `ActionParameterDef` has `type="struct"` and a
non-empty `fields` tuple of `StructFieldDef` values. Each inner declaration carries
`name`, a scalar `type` (`str`, `int`, `float`, `bool`, `date`, or `datetime`),
optional string `choices`, and `required`. A non-struct declaration has
`fields=None`. An optional outer model makes the property or parameter optional;
an optional inner annotation makes that inner field optional. An absent optional
inner field in a dict is stored as explicit `null`, so typed hydration supplies
`None` even if the model field has no default.

Writes accept a model instance or an equivalent dict and validate the declared
inner fields. A bad object write raises `INVALID_RECORD`; a bad action parameter
raises `INVALID_PARAMS`, with the message naming the inner path such as
`amount.currency`. A typed read returns the model, while string and MCP reads
return a plain object. Updating a struct replaces the whole value. Sensitivity
applies to the whole property.

Structs are flat: nested models, lists or maps of models, inner aliases, inner
property metadata, `RootModel`, and struct primary keys are refused at declaration.
Use `prop(property_type="json")` on a flat `BaseModel` annotation to store it opaque;
other explicit property types are refused. Querying by
an inner field is unsupported. A `where` condition on the struct property raises
`OPERATOR_TYPE_MISMATCH`; `order_by` raises `INVALID_PARAMS`; `group_by` raises
`INVALID_GROUP_BY`; and numeric aggregate functions raise
`NON_NUMERIC_AGGREGATE` (`count` is exempt). MCP's `list_object_types` and
`list_action_types` include `fields` on every property and parameter: `null` for
non-struct values, and a list of objects with `name`, `type`, `required`, and
`choices` for a struct.

#### Field markers: `ref`, `target`, `scope_ref`

All take an `OntologyObject` subclass and pass extra kwargs to `Field`.

| Marker | Declares |
| --- | --- |
| `ref(cls)` | This param refers to an object of `cls` |
| `target(cls)` | …and it is the action's **target** — scope is enforced against it. For an unscoped type there is no scope to enforce, so `roles=` is the only gate. One `target()` param must refer to the action's `target=` type |
| `scope_ref(cls)` | …and it names the **scope** the action creates within. It may not refer to an unscoped type (`SCOPE_POLICY_ERROR`) |

These drive `refers_to` / `scope_semantics` on the generated `ActionParameterDef`, so
scope enforcement is declared by the author rather than hardcoded in the engine.

#### `ActionParams`

Base class for typed action-params models. Fields use the markers above.

```python
class EscalateTicketParams(ActionParams):
    ticket_id: str = target(Ticket)
    reason: str | None = None
```

A `date` or `datetime` parameter follows the property rule in
[Date and datetime values](api-stores.md#date-and-datetime-values) and is refused with
`INVALID_PARAMS`; over MCP `execute_action` the value is a JSON string, so the
string rule applies.

#### Events: `Event`, `@ontology.event`, `emits=`, `ctx.emit`, `client.events`

An event is a business fact an action records, such as "order shipped". It is not
audit: audit says who did what, and an event says what happened. Declare an event
as an `Event` subclass. Its fields use the same type rules as `ActionParams`:
scalars, choices, and flat structs. `Sensitivity` works as it does on properties,
and a restricted field must be optional. An event has no primary key, no
transitions, and no `scope_level`.

```python
from ontary import Event

@ontology.event(description="An order has shipped.")
class OrderShipped(Event):
    carrier: str
```

`@ontology.event(*, description=None, api_name=None, accept=())` registers the class
as an `EventTypeDef`. The class must subclass `Event`, and one class may be decorated
once; otherwise the call raises `ONTOLOGY_INVALID`. `accept=` takes
`"EVENT_NEVER_EMITTED"`.

An action lists the events it may emit with `emits=[...]`. `ActionTypeDef.emits` holds
their api names, which MCP `list_action_types` shows as `emits`. A class in
`emits` that is not a registered event of the same `Ontology` raises
`ONTOLOGY_INVALID` at declaration.

```python
@ontology.action(ShipOrder, target=Order, roles=["ops"], emits=[OrderShipped])
def ship(ctx: ActionContext, p: ShipOrder) -> dict[str, Any]:
    order = ctx.get(Order, p.order_id)
    ...
    ctx.emit(OrderShipped(carrier=p.carrier))
    return {}
```

`ctx.emit(event, *, about=None)` records the event inside the action's transaction.
The subject defaults to the action's resolved target. When the action has no target
id, such as a creating action, pass `about=<object>`. That object must come from this
context (`get`, `all`, `create`, or `traverse`) and be of the action's target type.
One invocation may emit many events, including the same type twice; they are kept in
emission order. The event's `ts` is `ctx.now()`. Refusals raise inside the handler, so
the action rolls back and is audited as `error`:

| Case | Code |
| --- | --- |
| The event class is not registered on this `Ontology` | `UNKNOWN_NAME` |
| The event is not in the action's `emits` | `UNDECLARED_EVENT` |
| The subject cannot be resolved, is not of the target type, was not handed out by this context, or has no row yet | `EVENT_SUBJECT_INVALID` |

`client.events(event_type=None, /, *, about=None, since=None, until=None)` returns
the events the client's consumer may see, as `list[EventRecord[E]]`, in emission
order. `about` is an object or a `(cls, id)` tuple. `since` is inclusive and `until`
is exclusive; both must be timezone-aware. There is no paging. Passing an event class
hydrates each payload into that class; without one, each payload is a plain `Event`
that keeps its stored fields.

```python
records = client.events(OrderShipped, about=(Order, "o-1"))
records[0].payload.carrier
```

An event is visible when its subject's latest row passes the consumer's scope and
`row_visibility`. For a retired subject, that row is the last one before retirement.
After a subject moves scope, the new scope sees its whole history and the old scope
sees none. An event type no longer declared is hidden. Payload fields restricted by
`Sensitivity` for the consumer kind are `None` in the payload and listed in
`redacted_fields`.

`EventRecord` is frozen and generic. Its fields are `event_type`, `about_type`,
`about_id`, `ts`, `invocation_id`, `payload`, and `redacted_fields: frozenset[str]`.

Events are stored on the invocation's audit row, so they commit or roll back with
the action. `AuditEntry.events` lists them unredacted; see
[`AuditEntry`](api-reference.md#auditentry).

#### Advisory findings: `Finding.guide`, `accept`, and `snapshot`

`ontology.diagnose()` returns a `Finding` for each advisory code (the modelling
lints and the security lints listed in [the CLI reference](cli.md)). A `Finding` has
the fields `code`, `severity`, `location`, `message`, `fix_hint`, and `guide`.

- **`Finding.guide`** (`str | None`, default `None`) is the URL of the section of the
  published English design guide that explains the finding. It is
  `GUIDE_URL + "#" + anchor`, built from `ontary.diagnose.GUIDE_URL` and
  `ontary.diagnose.GUIDE_ANCHORS`. Every code in `ontary.diagnose.ADVISORY_CODES`
  sets it. Findings with no guide section (`ONTOLOGY_INVALID`, `SCOPE_POLICY_ERROR`,
  `INVALID_RECORD`, `RULE_VIOLATED`, `DIAGNOSE_RULE_FAILED`) leave it `None`.
- **`accept`** says "this is intentional" at the declaration that caused the finding.
  It takes one lint code or a sequence of codes. An accepted finding does not appear
  in `diagnose()`, in the `ontary validate` text, or in `--json`. A code that is not
  in the table below is refused (see the last bullet).

  | Declaration | `accept` argument | Codes it accepts |
  | --- | --- | --- |
  | `prop(...)` | `accept=` | `STORED_DERIVABLE`, `FREE_TEXT_STATUS` |
  | `@ontology.object(...)` | `accept=` | `FORBIDDEN_TYPE_NAME`, `AUDIT_TYPE` |
  | `@ontology.action(...)` | `accept=` | `CRUD_ACTION_NAME`, `MICRO_ACTION` |
  | `@ontology.function(...)` | `accept=` | `CRUD_ACTION_NAME` |
  | `@ontology.event(...)` | `accept=` | `EVENT_NEVER_EMITTED` |

  The accepted names are the `Literal` aliases `PropertyLint`, `ObjectLint`,
  `ActionLint`, `FunctionLint`, and `EventLint` in `ontary.meta`. They are not exported from
  `ontary`, but they make a wrong code a `mypy` error at the call site. The same
  `accept` field (`tuple[str, ...]`, default `()`) exists on `PropertyDef`,
  `ObjectTypeDef`, `ActionTypeDef`, and `FunctionDef`, so a hand-built descriptor
  behaves like a decorated class.
- **`snapshot`** (`bool`, default `False`) on `@ontology.object(...)` declares the
  type a point-in-time snapshot. It exempts the type from two findings: a
  `STORED_DERIVABLE` finding for any of its properties, and a `FORBIDDEN_TYPE_NAME`
  finding for a `Snapshot` name suffix. It does not exempt a `V<digits>`, year, or
  `History` name. A `*Snapshot` type without `snapshot=True` still warns. The text
  "declared snapshot" in `description` has no effect.
- **A refused code.** A code that a declaration cannot accept raises
  `ValidationFailed` with code `ONTOLOGY_INVALID` when the declaration is built. The
  message names the declaration, the refused code, and the codes that declaration
  accepts. This holds for an unknown code, a code for another kind of declaration,
  an error code, and a security lint code (`UNSCOPED_SENSITIVE`, `MIN_N_UNSET`).
  Lint codes are finding codes. They are never raised, so they are not in
  `ERROR_CODES` and not in the [error code table](api-reference.md#error-codes).

```python
@ontology.object(layer="L0", scope="unscoped", snapshot=True)
class AccountSnapshot(OntologyObject):
    id: str = prop(primary_key=True)
    health_score: int  # no STORED_DERIVABLE: the type is a declared snapshot

@ontology.object(layer="L0", scope="unscoped")
class Applicant(OntologyObject):
    id: str = prop(primary_key=True)
    credit_score: int = prop(accept="STORED_DERIVABLE")  # recorded from a bureau
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

## Descriptor authoring

The lower-level surface class authoring is built on. Useful for generated or
data-driven ontologies; most authors should use `Ontology`.

| Type | Key fields |
| --- | --- |
| `ObjectTypeDef` | `api_name`, `display_name`, `description`, `layer`, `properties`, `primary_key`, `rules`, `owned` |
| `PropertyDef` | `name`, `type`, `choices`, `fields`, `transitions`, `required`, `sensitivity`, `scope_level` |
| `LinkTypeDef` | `api_name`, `from_type`, `to_type`, `cardinality`, `description`, `identity_revealing`, `owned` |
| `ActionTypeDef` | `api_name`, `display_name`, `target_type`, `executable_by_roles`, `description`, `parameters`, `capabilities` |
| `ActionParameterDef` | `name`, `type`, `choices`, `fields`, `required`, `refers_to`, `scope_semantics` |
| `StructFieldDef` | `name`, `type`, `choices`, `required` |
| `TransitionDef` | `initial`, `moves` |
| `RuleDef` | `name`, `message`, `check` |
| `FunctionDef` | `api_name`, `description`, `input_description`, `output_description`, `parameters`, `capabilities` |
| `Sensitivity` | `ai_usable`, `human_visible` |

`PropertyType` is `Literal["str", "int", "float", "bool", "date", "datetime", "json", "struct"]`.

`StructFieldDef` describes one flat inner field. Its `type` is a scalar property
type, `choices` is an optional tuple of string values for a `str` field, and
`required` defaults to `True`. `PropertyDef.fields` and
`ActionParameterDef.fields` are non-empty tuples for `type="struct"` and are
`None` for every other type. The MCP schema renders each tuple as a list and
always includes the `fields` key.

`TransitionDef` describes a choice property's allowed states: `initial` is a
non-empty tuple of start states, and `moves` maps every choice to its allowed
targets. Use an empty target tuple for a terminal state. Every state must be a
declared choice. Set it on `PropertyDef.transitions`.

`RuleDef` describes a named predicate over the full new row as
`check: Callable[[dict[str, Any]], bool]`. Its `name` and `message` must be
non-empty, and names in one `ObjectTypeDef.rules` tuple must be unique. Rule
checks should read only the supplied object. `model_dump()` excludes `check`,
so the exported declaration contains only the rule's name and message.

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
undeclared; the provider itself is unsandboxed author code), `writeback`
(ontology writes are all ontology-owned; the runtime has no outward write path of its
own — a declared convention, not an enforced boundary, since a capability provider can
write outward inline), `reingest` (upsert-merge; owned properties survive; no
deletion), `visibility_default` (deny-by-default — unresolved scope hides),
`transaction_ownership` (runtime-owned — refuses caller-opened transactions),
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
is visible in the log; not every audited row has one. `min_n` is the one per-ontology answer, read off the
ontology's own `ScopePolicy.min_n`.

---

