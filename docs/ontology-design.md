# Ontology design

**English** · [日本語](ontology-design.ja.md)

This guide is for people and coding agents deciding what an ontology should mean
before they declare it with `Ontology` and `OntologyObject`, or generate the
lower-level `ObjectTypeDef`, `PropertyDef`, `LinkTypeDef`, `ActionTypeDef`, and
`FunctionDef` descriptors. It is a design guide, not a second API reference: use it
to choose boundaries, ownership, relationships, behavior, names, and security.

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

### Domain-driven design

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

When an existing object shape genuinely evolves, change the same type deliberately:
the stored `OntologyFingerprint` detects undeclared drift, and
`migrate_object_type` applies a declared version change and its upcasters. Do not
pre-build speculative extension points, and do not silently reinterpret an old
property to accommodate a new use case.

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
object explicitly, including its subject and observation time. Do not call an
ordinary cache, report result, or duplicated current value a snapshot. Routine
history needs no clone: stored rows already carry `valid_from` and `valid_to`.

Normalize semantic facts, not blindly every physical value. A property that is part
of an object's own state can stay on that object. Split it out when it has an
independent identity or lifecycle, many participants, distinct ownership, or
security that cannot be expressed cleanly on the containing object.

*Source: Palantir, "Ontology design: Structural guidance".*

### Structs

Foundry structs group nested fields inside a property. **No equivalent yet; the
nearest approximation is separate properties on the same `ObjectTypeDef`, or a
linked object type when the group has its own identity, lifecycle, reuse, or
security.** `PropertyDef` has no nested property-type declaration.

Choose separate properties for a small, inseparable value group that is always
created, secured, and changed with its parent. Choose another object type plus
`LinkTypeDef` when the group is repeatable, shared, independently governed, or a
target of actions. Avoid placing opaque nested data into one property merely to make
the descriptor shorter; doing so hides fields from ontology-level naming and
security review.

*Source: Palantir, "Ontology design: Structural guidance".*

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

Use `LinkTypeDef` for a relationship whose meaning is captured by its endpoints,
direction, `Cardinality`, and governance flags. Name the relationship from the
domain, validate both endpoints, and declare `owned` when an action rather than a
source creates the link. A domain example can use links to distinguish ordinary
containment, identity-revealing traversal, and action-owned relationships.

Foundry object-backed link types attach properties to a relationship. **No
equivalent yet; `LinkTypeDef` carries no payload properties, and the nearest
approximation is an explicit relationship `ObjectTypeDef` connected to each
participant by a `LinkTypeDef`.** Use that shape for facts such as role, effective
date, rank, or provenance that belong to the relationship itself. The relationship
object also becomes the correct target when its lifecycle needs an `ActionTypeDef`.

Do not encode the same association independently as an unconstrained foreign-key
property and a link unless the property is required for scope or contributor
resolution. If both are necessary, treat them as one invariant and populate them
together.

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

**In ontary:** keep a vendor-independent canonical staging model. Each connector
maps source data to canonical records, then canonical records to the ontology:
source → canonical → ontology. Use `Source` to preserve lineage and use `owned`
declarations to keep source-backed facts separate from ontology-owned state. Design
`ObjectTypeDef` and `LinkTypeDef` names from the domain, never from an extract.

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
transition, including its target, permitted roles, declared capabilities and effects,
and ontology-owned writes. Never expose generic create, update, or set-property
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
duplicate of a derivable fact. Schema evolution goes through
`migrate_object_type` and type `version` plus upcasters, never a `V2` type.

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
