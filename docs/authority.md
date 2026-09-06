# Authority, declarations, and architecture

[Back to the README](../README.md) · [API reference](api-reference.md)

This page combines the architecture narrative with the runtime's declared answers
and its “declared, not defended” boundary. They belong together: the data-flow
diagram shows where a decision is made, while the declarations say what that
decision means to an operator or auditor.

## Architecture

```text
authoring vocabulary (`ontary` root and canonical submodules)
        │ declares
core:   model / meta / typesys / errors / audit / security
        │ consumed by
runtime: ontology → OntologyRuntime → OntologyClient / BoundQuery
                     ├─ guarded reads: get, list, traverse, aggregate, aggregate_by
                     ├─ actions: role → scope → precondition → writes → audit
                     └─ Functions: guarded reads plus declared capabilities
        │ through the Store protocol
adapters: in-memory / SQLite / PostgreSQL stores
serving:  single- or multi-consumer MCP
ingest:   canonical staging → declarative mapping → ontology writes
```

An `Ontology` owns the registry, scope policy, object and link declarations,
actions, Functions, and capabilities for one domain. Binding it to a store
creates shared runtime machinery. `runtime.for_consumer(consumer)` creates a cheap
view over that machinery; it does not mutate global state or re-register handlers.

The store protocol separates persistence from policy. `OntologyClient` and MCP use
the guarded query layer before touching store rows. Actions use a transaction and
an action-write capture boundary before committing ontology-owned changes. The
connector path writes through ingest authority checks, while canonical staging
keeps source-specific records outside the domain model.

Failures share `OntaryError` and expose a stable `code` from `ERROR_CODES`. Callers
react to the kind-level classes `VisibilityError`, `PermissionDenied`,
`PreconditionFailed`, `ValidationFailed`, `AuthorityError`, `ConflictError`, and
`InternalError`; `ActionError` remains the handler-facing precondition vocabulary.
The root import is the curated authoring and runtime vocabulary, while engine-only
names stay available from their canonical submodules.

## Declared answers

The immutable `Declarations` value is available as `client.declarations` and through
the `get_declarations` MCP tool. Its strings are a runtime-readable contract, not a
replacement for behavior. `min_n` is read from the ontology's own scope policy;
the other answers describe engine-wide boundaries.

### Authority

Authority is `model-declared-runtime-checked`. An author marks each object, link,
or property as ontology-owned or leaves it source-backed by default. The action
write gate enforces that choice during a governed action, and ingest applies its
own per-record authority checks. A source-backed property cannot be changed by an
action merely because its handler has a store-like context.

Removal uses the same ownership declaration. A governed action may retire an
object only when its whole type declares `owned=True`, and may close a link only
when that link type declares `owned=True`. Attempting either operation on
source-backed data raises the authority-kind `UNDECLARED_SOURCE_REMOVAL`. If a
retirement cascade encounters an undeclared source-backed link after other links
have been closed, the action transaction rolls back the object retirement and
every earlier link closure.

### Capabilities

Capabilities are declared per action or Function, bound per client, resolved per
invocation, and fail closed when undeclared or unprovided. The provider is trusted
author code and is not sandboxed.

### Tenancy

There is one tenant per store instance. Objects, links, and audit entries are all
scoped to it; primary keys are unique per tenant. PostgreSQL can add
row-level security unless `rls=False`, but a database superuser can bypass RLS by
PostgreSQL design. See [storage and tenancy](storage.md).

### Write-back and failure semantics

Ontology-owned writes happen through governed actions. Source-backed state cannot be
mutated by an action, and the runtime has no outward write path of its own. A
capability provider may still perform an outward write inline because it is trusted
in-process code, so the convention is documented rather than enforced as a
sandbox.

### Re-ingest

Ingest is upsert-merge. A source refreshes only the source-backed properties it
supplies; ontology-owned values survive, and an object absent from a batch is not
deleted or snapshot-replaced.

### Visibility default

Visibility is deny-by-default. If scope resolution cannot place a row in the
consumer's scope chain, it is hidden rather than visible by omission. Scope
coverage currently matches exact scope ids; a parent-scoped consumer does not
automatically cover a child scope unless the policy explicitly resolves that
relationship.

### Transaction ownership

Transaction ownership is runtime-owned. `execute()` and ingest entry points refuse
to run inside a transaction already opened by the caller, so a successful
applied-and-audited action cannot later be rolled back by the caller.

### Read/write isolation

The outermost store transaction serializes a read followed by a write against
competing writers on the same store. SQLite uses an immediate transaction,
PostgreSQL uses a transaction-scoped advisory lock, and the in-memory backend keeps
the transaction under a re-entrant lock.

### Idempotency

Action retries are not deduplicated. Repeating identical parameters creates a new
audited attempt and applies a new write; callers that need to correlate their own
retry key must carry it in the declared parameters. An invocation id correlates
entries from one call, not separate calls.

### Audit scope

The audit view is administrative and tenant-scoped, not consumer-scoped or
redacted. Entries identify whether they came from an action or Function, and
Function auditing is enabled when the Function declares capabilities unless its
declaration overrides that default.

### Identity

On a multi-consumer MCP server, the transport proves the principal and a resolver
maps it to a `Consumer`; the SDK verifies no token and issues none. On a
single-consumer server or direct Python use, the operator asserts the `Consumer` at
construction and nothing proves it. The proven principal and resolved actor are
both audited. See [MCP serving](mcp-serving.md).

### min-N

`min_n` is read directly from the ontology's `ScopePolicy`. It is per-ontology
configuration, not a fixed global default, and it governs both aggregate release
and `count_contributors`.

## Declared, not defended

Two public boundaries are enforced by the client and serving surfaces, not by
in-process code that holds the raw store or executor handle:

- **Guarded reads.** `OntologyClient`, `BoundQuery`, and MCP reads apply scope,
  sensitivity, row visibility, and min-N before returning data. Trusted fixture
  loaders, tests, or scripts that hold a `Store` can read raw rows directly; the
  store is not a redaction boundary.
- **Action writes.** The action-write capture context covers the handler extent and
  enforces ownership on its insert, update, retire, link, and unlink operations.
  Trusted loaders or direct store writes outside an action are intentionally outside
  that gate. Ingest has its own authority checks and is not equivalent to an
  unrestricted raw write.

Neither boundary is an in-process sandbox. The defensible deployment shape is a
process that exposes only `OntologyClient` or MCP operations and never hands an
untrusted caller the store reference. That is the boundary this SDK declares and
the one the runtime can actually defend.

[Return to the README](../README.md) · [See the related API reference](api-reference.md)
