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

Products that call themselves an ontology split along one line: whether the
ontology **owns the writes**. On one side are analytics workspaces and
"context layers". They copy data out of source systems, derive a model of the
copy, and let people and agents read it and ask questions. They are useful,
and they are crowded: hosted platforms, cloud data warehouses, and several
open-source projects each offer one. On the other side is an **operational
ontology**: the model is where business verbs run. Every write is an action
with declared rules, every read and every action is checked against a
declared security policy, and every refusal is audited.

ontary is an operational ontology you can embed. It is a self-hosted Python
library on SQLite or Postgres. It pairs **actions that own a full state
transition** with **engine-enforced scope, sensitivity, and audit**, and it
serves the same governed model to people and AI agents over MCP. It fits one
developer's laptop and a team's deployed service alike. What it does not do is
copy data out of your SaaS tools or run analytics over the copy; feed it
through `client.ingest` from whatever already does that.

To the objection that this is "views and stored procedures": a view cannot
refuse a read, a stored procedure does not know who is asking or whether an
agent is the caller, and neither writes an audit row for the attempt it
refused. ontary does all three, declared once, enforced for every reader and
writer.

## M0 – M5 — shipped

Milestones M0 to M5 are closed. They shipped between v0.15.0 and v0.28.0 and
stay here as the record of what the engine already guarantees.

### M0 — Trust the core

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

### M1 — Model your operation

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

### M2 — Examples and docs

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

### M3 — Agent surface

Make the MCP tools honest and cheap for an agent.

- A list says when rows were filtered out, and a payload says which fields
  were redacted.
- Link traversal is paged and can return a count without the rows.
- List results carry `has_more` and a total.
- An action can carry a description written for agents.

### M4 — Refactor and performance

- Split the query layer into smaller modules.
- Share one store core across the in-memory, SQLite, and Postgres backends.
- Push `where` filters and scope checks down to SQL.
- Bulk ingest in one transaction, and batch link traversal.

### M5 — Harden the engine

Governance gaps and correctness bugs triaged from Later.

- `ref()` action parameters are scope-checked like `target()`, and the gate
  checks the id the handler will receive.
- Canonical ids in the action context, event payload validation, refusal
  codes for `list_events`, typed traverse paging, and an empty ingest that
  opens no transaction.

## The next phase — a team ships a product

M0 to M5 made ontary complete as an embeddable governed engine. The next
phase makes it complete as the substrate of a product: a service that many
people and agents use, built by a team, released more than once.

The done-test for this phase replaces the newcomer test: **a product team
deploys a multi-user ontary service and survives one ontology change in
production without dropping the store.** The first such team is the
maintainer's own; the first external team decides what comes after M9.

One decision underlies the phase. ontary is **the system of record for the
rows its actions write**. Ingested rows remain a governed projection of their
source systems and can be rebuilt from them; action-written rows and audit
history exist nowhere else. That is why schema evolution, as-of reads, and
event delivery are milestones now, not "Later".

## M6 — Name and measure

Cheap, compounding fixes before anything large, so every later milestone lands
on a repository whose one-line claim, numbers, and docs are already honest.

- A one-line positioning in the README and docs index that says "operational
  ontology" and answers the "views and stored procedures" objection with
  what the engine refuses, what it audits, and what an agent cannot do with
  raw SQL.
- A paste-into-your-coding-agent install prompt that ends at a validated
  ontology served over MCP.
- A benchmark harness and a published validation page with measured numbers
  for reads, traversal, actions, and bulk ingest on SQLite and Postgres.
- A Mermaid or DOT diagram exported from `Declarations`, so docs, pull
  requests, and agents get a picture of the model without an explorer app.
- `execute(..., idempotency_key=)`, so a retried action is not a second action.
- Docs drift fixed where the code moved on: the storage page's claim about
  payload JSON, and the "unenforced" comment on the principal stamp.
- Better `llms.txt` output.

## M7 — Serve a team

One deployed ontary process serves many proven identities across tenants
without serialising every request. This is the day-one requirement of any
product that is not one developer's laptop.

- A JWKS/OIDC token verifier adapter, plus helpers that map a verified subject
  to a `Consumer`. The resolver stays application code: ontary verifies tokens
  it did not issue and owns no user directory.
- Per-request tenant selection: a tenant-keyed store registry and a
  `resolve_store` hook on the multi-consumer server builder.
- Pooled Postgres connections per request, and blocking store calls moved
  off the event loop.
- A health endpoint and request counters on the ASGI app.
- Parent-covers-child scope, because a team deployment is where a
  manager-sees-team hierarchy first appears.
- Separate audiences for operating the service (ingest, administration) and
  acting on data, never both behind one consumer token.

## M8 — Reach the front end

A team builds a web UI and non-Python services on the same governed model,
and the model pushes events out instead of being polled.

- An HTTP API with OpenAPI generated from `Declarations`, exposing the same
  list, traverse, execute, and call surface as MCP with the same honest-result
  fields.
- A generated TypeScript client from the same `Declarations`, scope-aware
  paging included.
- One MCP tool per action and per function, plus MCP resources, so an agent
  learns a shape without a discovery round-trip.
- Event delivery: in-process subscribers with a stable event id first, then an
  outbox table with an at-least-once delivery worker.
- A staged-review mode in which an `ai` consumer's action becomes a proposal
  that a human approves before it commits.

## M9 — Evolve the model in place

A product's second release must not require dropping the store.

- A store-schema migration ladder, so a stamp bump upgrades in place, and an
  export/import tool for the cases it cannot.
- Ontology diff and data migration, additive changes and required-property
  backfill first.
- As-of reads of object history on the client; the rows are already kept.
- Rules that can see links, so a required assignment stops degrading to a
  string id field.
- A `FixedClock` that advances, which migration and as-of tests need.
- A written compatibility rule that caps store-schema bumps per minor release.

## Decided against

This section records features the project chose not to build, so the same design question is not reopened without new evidence.

- Links will not carry properties. A relationship that has its own facts (a time, role, or rank) is an object type linked to each participant. That object already gets identity, history, scope, actions, and audit, so no new engine concept is needed. See the [design guide](ontology-design.md#links-and-object-backed-link-types).
- No connectors or change capture. Copying data out of SaaS tools and
  databases is a product of its own, and the open-source projects that do it
  spend most of their effort there. `client.ingest` is the boundary; feed it
  from the tool you already use.
- No bundled language model, analytics workspace, or explorer application.
  Those live on the read side of the line above. ontary exports a diagram and
  a typed client; the application is yours.
- No second language runtime for the engine. Handlers are Python callables.
  The HTTP API and the generated TypeScript client are the portable artefacts.
- No "Palantir alternative" wording. The honest claim is the same governance
  guarantees at the read and action boundary, code-first and self-hosted,
  without the pipeline platform. Nothing more is claimed.
- No change of license. The MIT library you can vendor is part of the offer.

## Later — pulled by real use

These come back only when an example or a user needs them, one per line so a
page can point at each:

- Interfaces (polymorphic object types).
- Composing several operations into one model.

Four former Later items moved into milestones in the next phase: schema diff
and migration (M9), delivery of events to subscribers (M8), as-of reads of
history (M9), and parent-covers-child scope (M7).

To ask for one, open an issue that describes the operation that needs it.
