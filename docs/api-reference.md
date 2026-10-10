# `ontary` — API reference

**English** · [日本語](api-reference.ja.md) · [← README](../README.md)

*Reference* — This page lists the public names of `ontary` for lookup, and [Getting started](getting-started.md) and [Testing your ontology](testing.md) show them in use.

The curated front door of `ontary`: **46 names** in `__all__`. The rest of the
engine remains available from its canonical submodule (`ontary.meta`,
`ontary.store`, and so on).

This is a lookup document. For the narrative walkthrough — author, declare, bind,
read, serve — start with the [README](../README.md).

This page is the index of the API reference. The full entries live on seven pages:
[Authoring](api-authoring.md), [Runtime & clients](api-runtime.md),
[Reading](api-reading.md), [Actions & Functions](api-actions-functions.md),
[Stores & ingest](api-stores.md), [MCP server](api-mcp.md), and
[Error codes](api-errors.md).

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
includes `from ontary import *`, which fetches every `__all__` name. It also
includes `hasattr(ontary, "MCPServer")`: `hasattr` catches only
`AttributeError`, so it raises the same `ImportError` instead of returning
`False`. `dir(ontary)` lists `MCPServer` without importing the extra.

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

`Ontology.bind`, `OntologyRuntime`, and `OntologyClient` bind shared runtime machinery and consumer views, with typed and dynamic reads, hydration, and redaction behavior. Full reference: [Runtime & clients](api-runtime.md).

## Reading

`StoredObject`, `Lineage`, filters, ordering, bounded reads, `Page`/`TypedPage[T]` pagination, `traverse`, visible-row counting, aggregates, and `GuardedQuery`. Full reference: [Reading](api-reading.md).

## Actions

An action combines typed parameters with a handler declared by `@ontology.action`; `ActionContext` provides handler operations, authority governs writes and removals, and the `AuditEntry` record captures each attempt. Full reference: [Actions & Functions](api-actions-functions.md).

### `ActionContext`

See [Actions & Functions](api-actions-functions.md#actioncontext) for the handler context contract.

## Functions

`@ontology.function` declares derived computations using guarded reads through `BoundQuery`, including `query.now()`; the function audit boundary determines which calls are recorded. Full reference: [Actions & Functions](api-actions-functions.md).

## Capabilities

Declared capabilities provide outside-world dependencies to action and Function handlers, with providers bound on the runtime or per client and every use audited. Full reference: [Runtime & clients](api-runtime.md).

## Security

`Consumer` identifies the caller; scope enforcement, sensitivity redaction, min-N contributor thresholds, and identity-revealing link guards govern access. Full reference: [Runtime & clients](api-runtime.md).

## Stores

The `Store` protocol, the SQLite and Postgres backends, date/datetime value rules, and schema versioning. Full reference: [Stores & ingest](api-stores.md).

### Date and datetime values

See [Stores & ingest](api-stores.md#date-and-datetime-values) for the value write rules.

## Bulk ingest

`bulk_upsert`, `bulk_link`, client ingest methods, validation, and ingest reports. Full reference: [Stores & ingest](api-stores.md).

## MCP server

`build_mcp_server` exposes fourteen guarded tools for introspection, reads, actions, and functions; `build_multi_consumer_mcp_server` serves many verified identities through a shared runtime. Full reference: [MCP server](api-mcp.md).

## Descriptor authoring

Descriptor-based declaration, `Declarations` / `declarations(...)`, and legacy handlers. Full reference: [Authoring](api-authoring.md).

## Error codes

The stable `ERROR_CODES` registry, meanings grouped by seven kinds, and the code-count summary. Full reference: [Error codes](api-errors.md).

## Exception hierarchy

The public exception classes, their parents and raise conditions, and catching errors by stable code. Full reference: [Error codes](api-errors.md).
