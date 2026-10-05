# Roadmap

*Project* — This page describes where `ontary` is heading, and [Getting started](getting-started.md) shows how to build with it today.

ontary aims to be a genuinely useful SDK for building and running an
**operational ontology**: a model of your operations that people and AI
agents both work through.

One business operation is one domain. You describe it in the words you
already use: the things involved, the relationships between them, what
people do to them, and the rules that must hold. ontary turns that into a
sound domain model and a governed runtime. It enforces security, audit, and
consistency for every reader and writer.

## No DDD knowledge required

ontary is built on Domain-Driven Design, but you do not need to know DDD to
use it. The design is in the SDK, not in what you have to learn.

- **The API speaks the language of operations.** You declare things,
  relationships, actions, and rules. DDD terms appear only on one
  explanation page, for readers who already know them.
- **Good design is the default path.** The simple way to write something is
  also the sound way. When a model drifts, `validate()` and `diagnose()`
  say what to do instead, not only what is wrong.
- **We test it with newcomers.** A milestone is done when a developer with
  no DDD background builds a sound model from the docs alone.

Work is tracked as [GitHub Issues](https://github.com/ryoochi0112/ontary/issues),
grouped by [milestone](https://github.com/ryoochi0112/ontary/milestones).
The milestones run in the order below. A milestone lists an intent, not a
promise of dates. ontary is pre-1.0: any minor may break, and the
[CHANGELOG](changelog.md) is the migration guide.

## Where ontary fits

Hosted operational-ontology platforms exist, but you cannot embed them in
your own application. Semantic layers for analytics read data but do not
own state transitions. ontary is an embeddable, self-hosted Python library.
It runs on SQLite and Postgres. It pairs **actions that own a full state
transition** with **engine-enforced security and audit**, and it serves the
same governed model to AI agents over MCP.

## M0 — Trust the core

Correctness that every later milestone depends on.

- A primary key is unique in every store, and a second insert with the same
  key is refused.
- An action may target an `unscoped` type. Its `roles=` are the gate, and
  the audit entry records that the target was unscoped.
- A link refuses an endpoint that does not exist.
- Re-creating an existing many-to-many link is a no-op, not a duplicate.
- A `datetime` value is accepted on write, as it is returned on read.
- Authoring mistakes raise `OntaryError` with a catalogued code, not a bare
  `ValueError` or pydantic error.

## M1 — Model your operation

The core developer experience: the model you write reads like the
operation it describes, and the type checker catches mistakes.

- **A typed action context.** `ctx.get(Order, id)` returns an `Order`, and
  `order.status = ...; ctx.save(order)` is checked by `mypy`. The string-based
  context API was deprecated in 0.17.0 and removed in 0.18.0.
- **Choice properties** (Enum and `Literal`), and **structured properties**
  such as an amount with its currency, or a name in two languages.
- **Declared rules and status transitions** (for example, "an order can
  only be shipped once it is paid"), enforced on every write path, not
  hand-coded in each action.
- **Typed Function parameters**, like action parameters.
- **An injectable clock** that reaches handlers and stored history.
- **Events: a record of what happened.** An action declares the events it
  produces, such as "order shipped". They are stored in the action's
  transaction and readable from the client and the audit trail. Delivery to
  subscribers is not part of M1.
- **Test helpers in business terms**: given this state, when this action
  runs, then expect this result.
- **Checks that teach.** `validate()` and `diagnose()` catch the common
  modelling mistakes in the design guide and name the fix.

## M2 — Examples and docs

Written once, against the M1 API.

- Docs organised as tutorials, how-to guides, reference, and explanation.
- One tutorial that builds a domain end to end.
- An explanation page for readers who know DDD, mapping its terms to
  ontary, including what is not supported yet. No other page requires DDD
  vocabulary.
- A newcomer test: a developer with no DDD background models a new
  operation from the docs alone, and the result is reviewed against the
  design guide.
- More worked examples: order fulfilment (structured properties, status
  transitions, rules), scheduling (relationships that carry data), and a master-data
  graph (bulk load, traversal). The tickets example is brought in line with
  the design guide.
- A complete, tested MCP authentication example.
- `llms.txt` for AI-readable docs.

## M3 — Agent surface

Make the MCP tools honest and cheap for an agent.

- A list says when rows were filtered out, and a payload says which fields
  were redacted.
- Link traversal is paged and can return a count without the rows.
- List results carry `has_more` and a total.
- An action can carry a description written for agents.

## M4 — Refactor and performance

- Split the query layer into smaller modules.
- Share one store core across the in-memory, SQLite, and Postgres backends.
- Push `where` filters and scope checks down to SQL.
- Bulk ingest in one transaction, and batch link traversal.

## Decided against

This section records features the project chose not to build, so the same design question is not reopened without new evidence.

- Links will not carry properties. A relationship that has its own facts (a time, role, or rank) is an object type linked to each participant. That object already gets identity, history, scope, actions, and audit, so no new engine concept is needed. See the [design guide](ontology-design.md#links-and-object-backed-link-types).

## Later — pulled by real use

These come back only when an example or a user needs them:
schema diff and migration, delivery of events to subscribers,
as-of reads of history, interfaces (polymorphic object types),
composing several operations into one
model, and parent-covers-child scope.

To ask for one, open an issue that describes the operation that needs it.
