# Ontology design

**English** · [日本語](ontology-design.ja.md)

*Explanation* — This page explains how to think about ontology boundaries and security before you build, and [Getting started](getting-started.md) and the [API reference](api-reference.md) show the code.

This guide is for people and coding agents deciding what an ontology should mean
before they declare it with `Ontology` and `OntologyObject`, or generate the
lower-level `ObjectTypeDef`, `PropertyDef`, `LinkTypeDef`, `ActionTypeDef`, and
`FunctionDef` descriptors. It is a design guide, not a second API reference: use it
to choose boundaries, ownership, relationships, behavior, names, and security.
`ontary validate` reports the mistakes in this guide that a tool can detect, and each
finding links back to its section (see [the CLI reference](cli.md)).

It is an original, ontary-native distillation of Palantir Foundry's
[Best practices](https://www.palantir.com/docs/foundry/ontology/ontology-best-practices/),
[Structural guidance](https://www.palantir.com/docs/foundry/ontology/ontology-structural-guidance/),
and [Anti-patterns](https://www.palantir.com/docs/foundry/ontology/ontology-anti-patterns/).
Foundry's Workshop, Object Views, and MDOs are application or platform concepts and
are out of scope for an SDK; ontary has no corresponding authoring constructs.

A useful ontology does more than rename tables. It gives stable identities to
business objects, makes relationships explicit, exposes governed business
operations, derives answers through guarded reads, and declares which facts each
consumer may see. The result should still make sense if the source vendor, current
department chart, or user interface changes.

## Core principles

### Start from the domain's language

Start with the language and decisions of the domain, not the shape of an export.
An `ObjectTypeDef` should represent a business concept with a stable identity; a
`PropertyDef` should represent a fact about that concept; a `LinkTypeDef` should
name a relationship; an `ActionTypeDef` should express an allowed business
transition; and a `FunctionDef` should answer a derived question.

Work outward from real workflows. Identify the nouns people distinguish, the verbs
they are authorized to perform, the invariants those verbs protect, and the
questions users repeatedly calculate. In the
[tickets example](../examples/tickets/ontology.py), Org, Queue, Ticket, and
Comment are separate concepts because they have different identities and
lifecycles. A ticket-escalation action names a domain outcome rather than a
storage edit.

Keep physical integration behind that model. A source column may inform a property,
but it should not dictate an object boundary or public name.

For a term-by-term mapping of these ideas to ontary, see [the explanation page](coming-from-ddd.md).

*Source: Palantir, "Ontology design: Best practices".*

### Don't repeat yourself (rule of three)

Store one authoritative fact in one place and reach it through links or derive it
with a `FunctionDef`. Two similar shapes can remain separate while their meaning is
still emerging. On the third repetition, decide whether the shared idea is a real
object, a shared naming convention, or merely common implementation code. Abstracting
earlier often freezes an accidental similarity into the ontology.

DRY applies to meaning, not just field spelling. Copying a person's region onto
every related record creates several answers to the same question even if all
properties have the same type. Conversely, two properties named status need not be
unified if they describe different lifecycles. Use domain identity and ownership,
not textual similarity, to decide.

*Source: Palantir, "Ontology design: Best practices".*

### Open for extension, closed for modification

Prefer designs that accept a new workflow without changing the meaning of existing
types. New `ObjectTypeDef`, `LinkTypeDef`, `ActionTypeDef`, and `FunctionDef`
declarations can extend a stable core. Existing API names, property semantics, link
directions, and action outcomes are contracts for human applications and agents.

When an existing object shape genuinely evolves, change the same type deliberately
and keep its history in the store's own row history. Do not pre-build speculative
extension points, and do not silently reinterpret an old property to accommodate a
new use case.

*Source: Palantir, "Ontology design: Best practices".*

### Composition over deep hierarchies

Compose domain concepts with `LinkTypeDef` relationships rather than inventing a
deep inheritance tree. A link makes both endpoint identities, its direction, and its
`Cardinality` reviewable. It can also carry its own authority declaration through
`owned`.

Inheritance is an implementation technique for Python classes; it is not a substitute
for domain relationships. Prefer small object types that can participate in several
links. When several types need a common derived question, a `FunctionDef` can read
across them without claiming that they share one ontological parent.

*Source: Palantir, "Ontology design: Best practices".*

## Structural guidance

### Normalization and derived values

Record each fact at the boundary that owns it. Link to that fact from elsewhere
instead of copying it into every object that needs it. In particular, store inputs
once and calculate scores, rollups, classifications, and other derived answers with
`FunctionDef`; Functions use guarded reads and do not write. The tickets example's
[ticket statistics Function](../examples/tickets/ontology.py) derives aggregates
from Ticket facts rather than persisting competing totals.

A declared point-in-time snapshot is the one intentional exception. If the domain
must preserve what a score or decision was at a named instant, model a snapshot
object explicitly, including its subject and observation time, and declare it with
`snapshot=True` on `@ontology.object`. Do not call an
ordinary cache, report result, or duplicated current value a snapshot. Routine
history needs no clone: stored rows already carry `valid_from` and `valid_to`.

Normalize semantic facts, not blindly every physical value. A property that is part
of an object's own state can stay on that object. Split it out when it has an
independent identity or lifecycle, many participants, distinct ownership, or
security that cannot be expressed cleanly on the containing object.

*Source: Palantir, "Ontology design: Structural guidance".*

### Structs

Use a struct for a small, inseparable value that is always created, secured, and
changed as a whole with its parent. For example, an order can declare `amount: Money`:

```python
class Money(BaseModel):
    value: float
    currency: str


class Order(OntologyObject):
    amount: Money
```

The inner fields remain part of the ontology declaration, while sensitivity applies
to the whole value.
Struct inner fields may only default to None; set other values at the call site.

Use a linked object type with a `LinkTypeDef` when the group is repeatable, shared,
independently governed, or an action target. These cases need their own identity,
lifecycle, ownership, or action boundary.

Structs are flat. Filtering, ordering, and grouping on the struct property are
unsupported; numeric aggregation is also unsupported, though count remains
available. Querying by an inner field such as `amount.currency` is not supported.

*Source: Palantir, "Ontology design: Structural guidance".*

### Choice properties

A fact that takes one of a fixed set of values, such as a ticket's status, is a
choice property: annotate it with a string Enum or a Literal. The `PropertyDef` then
declares the allowed values, and every write path enforces them. Do not accept free
text and check the value inside each action.

A choice is a value, not an entity. When the values need their own attributes,
lifecycle, or security, or people add new values as part of the operation, model
them as an `ObjectTypeDef` and link to it with a `LinkTypeDef` instead. Changing the
set of choices is a schema change. Removing a value leaves stored rows that the
narrower declaration refuses on read, so plan that change like any other migration.

### Rules and status transitions

Use a transition graph for the allowed moves of one choice property. Use a rule
for an invariant that can be decided from one object, and use an action
precondition for an operation-specific condition such as permission, request
context, or another object. A cross-object check stays in the action; a rule is
not a place to look up other objects.

```python
from enum import StrEnum

from ontary import Ontology, OntologyObject, prop
from ontary.meta import TransitionDef

_ontology = Ontology("orders", scope_levels=["team"])


class OrderStatus(StrEnum):
    PENDING = "pending"
    PAID = "paid"
    SHIPPED = "shipped"


@_ontology.object(layer="L0", scope="unscoped")
class Order(OntologyObject):
    id: str = prop(primary_key=True)
    status: OrderStatus = prop(transitions=TransitionDef(
        initial=("pending",),
        moves={"pending": ("paid",), "paid": ("shipped",), "shipped": ()},
    ))
    paid: bool = False


@_ontology.rule(Order, "shipped_needs_payment", message="payment is required")
def shipped_needs_payment(order: Order) -> bool:
    return order.status != OrderStatus.SHIPPED or order.paid
```

Keep each rule pure: the predicate should decide from the typed object it
receives and should not write data or consult external state. It receives the
full new object with every declared property, including properties restricted
from consumers. The engine does not enforce purity. A failed rule refuses the
write, and the refusal includes the rule name and its message.

The engine checks both on every write. A refused move names the current state,
the requested state, and the allowed moves; a refused rule names the rule and
its message:

```text
TRANSITION_NOT_ALLOWED: Order 'o-1': status cannot move from 'pending' to 'shipped'; allowed from 'pending': ['paid']
RULE_VIOLATED: Order 'o-1': rule 'shipped_needs_payment': payment is required
```

Each line shows the error code, then its message. The message, `str(exc)`,
does not include the code; read the code from `exc.code`.

An action assigns the new status and saves. It does not pre-check the declared
moves or rules: the engine refuses the write, and tests should expect the
engine's code (`TRANSITION_NOT_ALLOWED` or `RULE_VIOLATED`).

The `initial` states apply to objects created through an action. Ingest and
direct store inserts may start at any declared state. An ingest update of an
existing row must still follow the transition graph; it cannot move that row to
a state the graph does not allow.

### Events

An event is a business fact: "the order shipped". Readers see it under the same
object security as the subject it is about. Audit is a different record. It is the
administrative who-did-what trail of the engine, and an event is what happened.
Record the fact as an event; do not read the audit log to learn it.

Name an event in the past tense, because it states something that already
happened: `OrderShipped`, not `ShipOrder`. The imperative name belongs to the
action that causes it.

Do not declare an `*Event` object type to hold facts. That stores the same fact
twice and escapes the engine's visibility rules. Declare an `Event` and let an
action emit it with `emits=`.

An event is about the action's target. Pass `about=` to `ctx.emit` when the action
has no target id (a creating action) or when the fact concerns another object of
the action's target type that the context handed out. Events share the retention
of the audit row they are stored on: they commit or roll back with the invocation
and live as long as that row.

```python
from typing import Any

from ontary import ActionContext, ActionParams, Event, Ontology, OntologyObject, prop, target

_ontology = Ontology("orders", scope_levels=["team"])


@_ontology.object(layer="L0", scope="unscoped")
class Order(OntologyObject):
    id: str = prop(primary_key=True)


@_ontology.event(description="An order has shipped.")
class OrderShipped(Event):
    carrier: str


class ShipOrder(ActionParams):
    order_id: str = target(Order)
    carrier: str


@_ontology.action(ShipOrder, target=Order, roles=["ops"], emits=[OrderShipped])
def ship(ctx: ActionContext, p: ShipOrder) -> dict[str, Any]:
    ctx.get(Order, p.order_id)
    ctx.emit(OrderShipped(carrier=p.carrier))
    return {}
```

### Interfaces

Foundry interfaces define a common contract across object types. **No equivalent
yet; the nearest approximation is shared property conventions plus Functions over
multiple types.** There is no ontary interface declaration and no promise that two
`ObjectTypeDef` instances are substitutable.

If several types expose the same concept, give the relevant `PropertyDef`
declarations the same meaning and naming, document that convention, and validate it
in the ontology's own tests. Use a `FunctionDef` when consumers need one derived
operation over multiple types. If shared identity and lifecycle emerge, reconsider
whether the types should instead link to one common object.

*Source: Palantir, "Ontology design: Structural guidance".*

### Links and object-backed link types

A relationship that has its own facts, such as a time, role, rank, or provenance,
is an object type linked to each participant. Foundry calls this an object-backed
link type.

Use a plain `LinkTypeDef` when the endpoints, direction, and cardinality describe
the whole relationship. Use a relationship object when the relationship has facts
or its own lifecycle.

Name each link from the domain. Declare `owned` when an action, not a source,
creates the link.

```python
from datetime import datetime
from typing import Any

from ontary import (
    ActionContext,
    ActionError,
    ActionParams,
    BoundQuery,
    Cardinality,
    FunctionParams,
    LinkHandle,
    Ontology,
    OntologyObject,
    prop,
    ref,
    target,
)

_ontology = Ontology("scheduling", scope_levels=["team"])


@_ontology.object(layer="L0", scope="unscoped")
class Session(OntologyObject):
    id: str = prop(primary_key=True)


@_ontology.object(layer="L0", scope="unscoped")
class Room(OntologyObject):
    id: str = prop(primary_key=True)


@_ontology.object(layer="L0", scope="unscoped", owned=True)
class Assignment(OntologyObject):
    id: str = prop(primary_key=True)
    starts_at: datetime
    ends_at: datetime


assignment_session: LinkHandle[Assignment, Session] = _ontology.link(
    "assignment_session", Assignment, Session, Cardinality.MANY_TO_ONE, owned=True
)
assignment_room: LinkHandle[Assignment, Room] = _ontology.link(
    "assignment_room", Assignment, Room, Cardinality.MANY_TO_ONE, owned=True
)


class ScheduleSession(ActionParams):
    session_id: str = target(Session)
    room_id: str = ref(Room)
    starts_at: datetime
    ends_at: datetime


@_ontology.action(ScheduleSession, target=Session, roles=["ops"])
def schedule_session(ctx: ActionContext, p: ScheduleSession) -> dict[str, str]:
    session = ctx.get(Session, p.session_id)
    if session is None:
        raise ActionError("session does not exist", code="PRECONDITION_FAILED")
    assignment = ctx.create(Assignment, starts_at=p.starts_at, ends_at=p.ends_at)
    ctx.link(assignment_session, assignment, session)
    ctx.link(assignment_room, assignment, p.room_id)
    return {"assignment_id": assignment.id}


class SessionPlacement(FunctionParams):
    session_id: str = ref(Session)


@_ontology.function(SessionPlacement)
def session_placement(
    query: BoundQuery, p: SessionPlacement
) -> dict[str, Any] | None:
    assignments = query.traverse(assignment_session, p.session_id, reverse=True)
    if not assignments:
        return None
    assignment = assignments[0]
    room = query.traverse(assignment_room, assignment.id)[0]
    return {
        "room_id": room.id,
        "starts_at": assignment.starts_at,
        "ends_at": assignment.ends_at,
    }
```

If the room is unknown, ctx.link refuses it and the whole action rolls back.
Moving a session is its own business action that retires the old Assignment.

The engine scope-checks a `target()` parameter, but not a `ref()` parameter.
This sketch is unscoped, so that is safe here. Once Session and Room are
scoped, a caller could pass a room from another scope. Check in the handler that
the room shares the session's scope, and refuse with `PRECONDITION_FAILED`
otherwise. [examples/room_booking](../examples/room_booking/README.md) shows the
same-building check.

Do not encode the same association independently as an unconstrained foreign-key
property and a link unless the property is required for scope or contributor
resolution. If both are necessary, treat them as one invariant and populate them
together.

For a complete worked example of this pattern, see [examples/room_booking](../examples/room_booking/README.md).

*Source: Palantir, "Ontology design: Structural guidance".*

### Naming conventions

Use singular business nouns for object types, specific relationship phrases for
links, business verbs for actions, and question-like or result-oriented names for
Functions. Names should make sense to a domain expert without knowing a database
schema, vendor API, team acronym, or current UI.

Treat public API names as durable identifiers. Keep display text and descriptions
clear enough for an agent to choose the right operation, but do not overload one
name with several meanings. Use consistent property names only where the underlying
semantics are consistent. Avoid implementation suffixes such as V2, table prefixes,
and temporary project labels.

*Source: Palantir, "Ontology design: Structural guidance".*

### Retirement and removal

Removal is a business verb. Model it as an `ActionTypeDef` named for the business
outcome, such as `OffboardEmployee` or `CancelSubscription`; never name the action
`DeleteEmployee` or `RemoveEmployee`. Inside the declared action's handler, call the
engine primitives `ActionContext.retire` and `ActionContext.unlink` to perform the
transition. The action name stays in domain language, and there is no generic
delete/remove action to expose.

`Store.retire_object` closes the current row by setting `valid_to`; it is not a delete.
Lineage, history, and audit survive. A retired object is absent from current reads but
remains present in history. `ActionContext.retire` also cascade-closes every live link
that references the object on the side its link type declares for that object type, in
the same transaction.

Erasure is not an ontology concept, and this SDK gives you no erasure verb. Retirement
is the lifecycle verb: do not declare an `Erase*` or `Delete*` action, Function, client
operation, or MCP tool to serve a right-to-erasure request under GDPR/APPI. Destroying
stored bytes is an operational matter for whoever runs the database, decided outside the
declared ontology and separate from the business action that retires an object. Audit
stays the engine's `AuditEntry`; never declare a type or an action to record it.

A governed action may retire an object only when its whole type declares `owned=True`,
and may close a link only when that link type declares `owned=True`. Partial ownership
(an `owned` property map) is not enough. Source-backed data can never be retired or
closed by an action; the attempt raises the authority refusal with code
`UNDECLARED_SOURCE_REMOVAL`. If a retirement cascade meets a source-backed link, the
whole action transaction rolls back—there is no partial cascade.

### Security design

Security is part of the semantic model, not a filter added by each application.
Design the `ScopePolicy` with the object and link graph: its `row_visibility` rules
decide whether a whole record exists for a consumer, while `min_n` prevents an
aggregate from describing too few distinct contributors. A failed aggregate raises
`VisibilityError` with code MIN_N_VIOLATION; an invisible record or forbidden
sensitive operation raises `VisibilityError` with code VISIBILITY_DENIED.

Declare field semantics on `PropertyDef`: `scope_level` participates in scope
resolution, and `sensitivity` holds a `Sensitivity` policy. Use `ai_usable` to hide
data from an AI consumer and `human_visible` to hide data from a human consumer.
These are different decisions, not a single confidentiality flag. Restricted
properties must remain optional so redaction can return no value.

Test the combinations, not just each declaration alone. In an HR domain, a policy
might hide candidate email from AI, hide demographic and identity fields from
humans, remove confidential rows, and count distinct candidates for min-N. That is
the intended shape: one
`ScopePolicy` enforced for every consumer, with semantic redaction by consumer kind.

*Source: Palantir, "Ontology design: Structural guidance".*

## Anti-patterns

### System Silos

**Anti-pattern:** shaping the ontology around one vendor, connector, or source
system, so its table names, identifiers, and quirks become the public domain model.

**Why it fails:** replacing a vendor becomes an ontology migration; consumers learn
integration details; and the same real-world entity arrives as several
source-specific objects with no stable identity.

**In ontary:** keep the ontology vendor-independent. Name `ObjectTypeDef` and
`LinkTypeDef` from the domain, never from an extract. Load source data through
`OntologyClient.ingest`, which maps each incoming record onto those declared types
rather than letting the extract's own shape through. Use `Source` to preserve
lineage, and use `owned` declarations to keep source-backed facts separate from
ontology-owned state.

For a worked example of loading source data through `OntologyClient.ingest` with `Source` lineage, see [examples/bill_of_materials](../examples/bill_of_materials/README.md).

*Source: Palantir, "Ontology design: Anti-patterns".*

### The Kitchen Sink

**Anti-pattern:** putting every available column and every possible concern onto one
object type because the source can produce them.

**Why it fails:** most properties become optional or ambiguous, unrelated lifecycles
collide, security becomes coarse, and every consumer must understand a sprawling
shape to use a small part of it.

**In ontary:** keep an `ObjectTypeDef` cohesive around one identity and lifecycle.
Move independently governed or repeated concepts to their own object types and join
them with `LinkTypeDef`. Keep `PropertyDef` declarations that are genuinely facts of
the object, and derive consumer-specific answers with `FunctionDef` rather than
adding report-shaped fields.

*Source: Palantir, "Ontology design: Anti-patterns".*

### Department Silos

**Anti-pattern:** declaring separate versions of the same business entity for each
department, workflow, or access group.

**Why it fails:** identity and facts drift, cross-department links require matching
logic, and organizational changes force schema changes even though the underlying
domain did not change.

**In ontary:** declare one `ObjectTypeDef` for one domain identity and connect
department-specific process objects with `LinkTypeDef`. Govern who can see the
shared entity through `ScopePolicy`, `PropertyDef` sensitivity, and
`row_visibility`, not by duplicating it. Put a department's distinct behavior in
appropriately scoped `ActionTypeDef` declarations.

*Source: Palantir, "Ontology design: Anti-patterns".*

### The God Object

**Anti-pattern:** making one central object own nearly every property, link, action,
and lifecycle in the domain.

**Why it fails:** unrelated changes contend on one schema, cardinalities become
implicit collections of fields, permissions spread across exceptions, and a change
for one workflow risks every consumer of the object.

**In ontary:** separate concepts with stable identities into focused
`ObjectTypeDef` declarations and compose them through explicit `LinkTypeDef`
relationships with reviewed `Cardinality`. Target each `ActionTypeDef` at the object
whose lifecycle it changes. Keep the graph connected, but do not confuse connection
with ownership by one root object.

*Source: Palantir, "Ontology design: Anti-patterns".*

### The Golden Hammer

**Anti-pattern:** representing every concern as another ontology object type,
including logs, derived metrics, runtime metadata, and infrastructure mechanisms.

**Why it fails:** the graph fills with implementation artifacts, consumers cannot
distinguish domain state from engine state, and duplicated platform records acquire
their own inconsistent lifecycle and security rules.

**In ontary:** choose the construct that matches the concern: `PropertyDef` for a
fact, `LinkTypeDef` for a relationship, `FunctionDef` for a derived answer, and
`ActionTypeDef` for a governed transition. Audit is the engine's append-only
`AuditEntry`; never declare an audit object type to mirror it. Keep Workshop, Object
Views, and MDOs out of the ontology because they are application/platform concerns,
not SDK domain constructs.

*Source: Palantir, "Ontology design: Anti-patterns".*

### Action Sprawl

**Anti-pattern:** generating a setter or CRUD action for every mutable property and
calling that an operational ontology.

**Why it fails:** callers must reconstruct workflows from low-level edits,
preconditions and permissions drift across setters, partial updates become possible,
and audit records describe storage mechanics instead of business intent.

**In ontary:** declare a small `ActionTypeDef` set named with business verbs and
aligned to real outcomes. One action should own the complete invariant-preserving
transition, including its target, permitted roles, declared capabilities, and
ontology-owned writes. Never expose generic create, update, or set-property
actions. The tickets example's [ticket-escalation action](../examples/tickets/ontology.py)
escalates a ticket; its name tells a reviewer what happened.

*Source: Palantir, "Ontology design: Anti-patterns".*

### The Time Machine

**Anti-pattern:** modeling history as separate objects or object types —
`Survey2024`, `SurveyResponseV2`, an `*History` clone per entity.

**Why it fails:** every consumer must know which version to query; links fan out
across clones; "current state" becomes a convention instead of a query.

**In ontary:** declare one object type; the store keeps row history
(`valid_from`/`valid_to`, close-old-insert-new), so history is a storage concern,
not a modeling problem. If a point-in-time value must be first-class, declare it
explicitly as a snapshot type (e.g. `EngagementScoreSnapshot`) — the only sanctioned
duplicate of a derivable fact. History is row history, never a `V2` type.

```python
@ontology.object(layer="L0", snapshot=True)
class EngagementScoreSnapshot(OntologyObject):
    id: str = prop(primary_key=True)
```

*Source: Palantir, "Ontology design: Anti-patterns".*

### The Misnomer

**Anti-pattern:** using a familiar but inaccurate business word, a source-system
label, or a vague technical name for an object, link, action, or property.

**Why it fails:** different consumers attach different meanings to the same
declaration, agents select the wrong operation from its description, and later
authors compensate with aliases and exceptions instead of fixing the model.

**In ontary:** make each `ObjectTypeDef`, `LinkTypeDef`, `ActionTypeDef`, and
`FunctionDef` API name state one precise domain meaning. Use descriptions to record
boundaries and invariants, and use business verbs for actions. If a name is wrong,
plan an explicit schema and consumer migration; do not create a misleading V2 twin
or silently change what the old name means.

*Source: Palantir, "Ontology design: Anti-patterns".*
