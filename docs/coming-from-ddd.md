# Coming from Domain-Driven Design

[English](coming-from-ddd.md) · [日本語](coming-from-ddd.ja.md)

*Explanation* — This page maps familiar terms to ontary. The [design guide](ontology-design.md) explains design choices, and [Getting started](getting-started.md) shows the code.

You do not need Domain-Driven Design (DDD) to use ontary.
This page helps readers who already know DDD find the corresponding operations and limits.
Some terms have a nearest equivalent rather than a declared construct.

## Term by term

| DDD term | In ontary | What it means here |
| --- | --- | --- |
| Ubiquitous language | Business names on `OntologyObject`, actions, and Functions | Name things, relationships, operations, and answers with the words people use in the operation. → [Getting started](getting-started.md) |
| Bounded context | Not supported — nearest: `Ontology` for one operation | An ontology declares one operation's model; it does not declare a bounded context or relationships between contexts. → [Authoring an ontology](api-reference.md#authoring-an-ontology) |
| Context map | Not supported | Links connect objects within a model; they do not declare relationships between bounded contexts. → [Links and object-backed link types](ontology-design.md#links-and-object-backed-link-types) |
| Entity | `OntologyObject` with a primary key | A declared object has stable identity through its primary key, and the store keeps its row history. → [Getting started](getting-started.md) |
| Value object | A struct property (`prop` with a struct type); choice properties for enumerated values | A struct holds a value without its own identity, such as an amount with its currency. → [Struct properties and parameters](api-reference.md#struct-properties-and-parameters) |
| Aggregate | Not declared — nearest: one action per state transition in one transaction, plus rules for single-object facts | ontary has no aggregate boundary or root. An action owns a whole state transition in one transaction. A rule checks a fact decided from one object. Cross-object checks stay in the action. → [Rules and status transitions](ontology-design.md#rules-and-status-transitions) |
| Aggregate root | Not declared — nearest: an action's target | An action's target identifies the subject of an operation; it does not declare an aggregate root or boundary. → [Actions](api-reference.md#actions) |
| Invariant | `Ontology.rule` and `prop(transitions=...)` | Rules check facts decided from one object, and transition graphs restrict the allowed moves of a choice property. → [Rules and status transitions](ontology-design.md#rules-and-status-transitions) |
| Domain event | `Event`, `emits=`, and `ctx.emit` | An action stores a business fact in its transaction. Delivery to subscribers is not supported yet; it is Later. → [Events](ontology-design.md#events) |
| Repository | Not a user construct — nearest: the engine's store and `OntologyClient` reads | The store persists objects, while client reads apply the consumer's security policy. → [Getting started](getting-started.md) |
| Factory | Not supported — nearest: `ActionContext` creation inside an action | Create an object through an action's context when creation is a business operation. → [ActionContext](api-reference.md#actioncontext) |
| Domain service | Function for a derived answer, or action for a state change | A Function derives an answer through guarded reads; an action owns a state transition. → [Functions](api-reference.md#functions) |
| Application service | Not a separate construct — nearest: `OntologyClient` invoking actions and Functions | The client invokes governed operations for a consumer, and the engine applies security and audit. → [Getting started](getting-started.md) |
| Specification | Not a declared construct — nearest: `where` filters or a Function | Filters select matching rows; a Function can derive a decision from guarded reads. → [Functions](api-reference.md#functions) |
| Anti-corruption layer | Not supported — nearest: application mapping before `OntologyClient` ingest | Map source records onto declared types before ingest; ontary does not declare an integration translation layer. → [Bulk ingest](api-reference.md#bulk-ingest) |

## Not supported

The table's missing declarations do not imply plans to add them.
The roadmap records these specific limits and decisions:

- **Later** — Delivery of events to subscribers. Events currently remain stored facts. → [Roadmap](roadmap.md#later--pulled-by-real-use)
- **Later** — As-of reads of history. The store keeps row history, but client reads expose current objects. → [Roadmap](roadmap.md#later--pulled-by-real-use)
- **Later** — Interfaces and polymorphic object types. Shared property conventions do not declare substitutable types. → [Roadmap](roadmap.md#later--pulled-by-real-use)
- **Later** — Composing several operations into one model. There is no declared context map. → [Roadmap](roadmap.md#later--pulled-by-real-use)
- **Decided against** — Links with properties. Use an object type linked to each participant when a relationship carries facts. → [Roadmap](roadmap.md#decided-against)

## Related pages

- [Getting started](getting-started.md) builds a small ontology.
- [Ontology design](ontology-design.md) explains ownership, relationships, rules, events, and security.
- [API reference](api-reference.md) documents declarations and runtime behavior.
- [Testing your ontology](testing.md) checks state changes and refusals.
- [Roadmap](roadmap.md) records future work and decisions.
