# Storage backends, tenancy, and schema

[Back to the README](../README.md) · [API reference](api-reference.md)

The runtime depends on the `Store` protocol, not on a particular database. Choose
the backend for the deployment shape, then bind the ontology once and hand out
consumer views from that runtime. Storage does not replace the guarded client
surface: direct store access is trusted application code, while reads through an
`OntologyClient` or MCP server remain scope-, sensitivity-, and min-N-aware.

## Choose a backend

| Backend | Best fit | Import |
| --- | --- | --- |
| `InMemoryStore` | Fast tests and dependency-free doubles | `ontary` |
| `ObjectStore` | A local file, script, or single-process application | `ontary` |
| `PostgresStore` | A deployment with multiple application processes | `ontary` |

All three implement the same store contract. The action, ingest, and guarded-read
layers therefore do not need backend-specific branches. A typical local runtime is:

```python
from ontary import ObjectStore

store = ObjectStore(ontology.registry, path="ontary.sqlite")
runtime = ontology.bind(store)
```

For a test double, import the backend from the front door:

```python
from ontary import InMemoryStore

store = InMemoryStore(ontology.registry)
```

PostgreSQL is selected by the optional `postgres` extra and keeps the same runtime
construction:

```python
from ontary import PostgresStore

store = PostgresStore(
    ontology.registry,
    "postgresql://localhost/ontary",
    tenant="acme",
)
runtime = ontology.bind(store)
```

The PostgreSQL implementation has a few intentional differences from SQLite:

- SQLite supports its historical migration path because local store files can
  predate the current engine. PostgreSQL creates the current schema and refuses a
  database whose stamp does not belong to this engine rather than silently adopting
  an unknown layout.
- Object payloads are stored as `TEXT`, matching the other backends. The engine does
  not query inside payload JSON, so JSONB normalization would buy no runtime
  behavior while making byte-level parity harder: the dominant linear cost is
  Python-side scope resolution, not SQL scanning.
- Outbox claims use PostgreSQL row locking with `SKIP LOCKED`, preserving the same
  lease semantics when drainers run in separate processes.
- Row-level security can provide a database-side tenancy check in addition to the
  engine predicates. It is defense in depth, not a replacement for the store's
  tenant binding.

The PostgreSQL integration suite is optional locally. `make verify` remains an
offline gate; an absent PostgreSQL service is an expected skip, not a reason to
change the test configuration.

## Tenancy is bound to the store

A store instance represents one tenant. Pass that tenant when constructing the
store; there is no request-scoped tenant switch:

```python
acme = PostgresStore(ontology.registry, dsn, tenant="acme")
globex = PostgresStore(ontology.registry, dsn, tenant="globex")

acme.insert("Widget", {"id": "w-1"}, source)
assert globex.read_current("Widget", "w-1") is None
```

The boundary covers objects, links, audit entries, and durable effect-outbox rows.
Primary keys are unique within a tenant rather than across the whole deployment,
so two tenants may use the same domain identifier without colliding. The tenant is
fixed at construction precisely because a per-call argument is easy to omit and a
missed argument would have a cross-tenant failure direction.

On PostgreSQL, `rls=True` creates `FORCE` row-level-security policies keyed by the
current session tenant. An unset session setting matches nothing. A superuser can
bypass PostgreSQL RLS by database design, so application connections must use an
ordinary role. `rls=False` disables only this second database layer; the engine's
tenant predicates still run.

Database- or schema-per-tenant is also valid when stronger physical isolation is
worth the operational cost. The shared-schema option is for deployments where a
single database is the better fit.

## Schema version compatibility

SQLite stamps each file with `SCHEMA_VERSION` through `PRAGMA user_version` when it
creates it; Postgres records the same number in `schema_meta`. Neither backend
carries a migration ladder, so any other stamp — higher, lower, or an unstamped store
that already has an `objects` table — is refused at construction with `ConflictError`
and code `STORE_VERSION_UNSUPPORTED`, naming the engine and store versions. Moving a
store across schema versions is an explicit operator step: open it with the matching
ontary version, or migrate the data into a fresh store.

The SDK simplification does not bump the schema version. Existing store files stay
on the compatible schema path; check [the compatibility policy](compatibility.md)
before changing an ontology definition or moving a store between SDK versions.

## Operator erasure runbook

For a data-subject erasure request, run `ontary erase pkg.module:ontology
--store ontary.sqlite --type Person --id person-123 --operator privacy-ops`.
The command purges content from the object's current and historical rows, matching
audit parameters, and effect-outbox payloads while structure and lineage survive
in audited tombstone rows; this matching is structural, not a completeness
guarantee. Detection reaches a bare `id`, the generic `object_id`/`object_ids`/
`target_id`/`target_ids` pairs, and `*_id`/`*_ids` keys whose trailing
underscore-separated stem segments, concatenated case-insensitively, name the
erased object type; a mismatched paired `*_type` suppresses the match. It does
not reach `owner_id`, `actor_id`, or `assignee_id` on an unrelated stem, prose
mentions of the subject in `params`, free text in `EffectRecord.error`, or free
text in outbox `last_error`. Commit `5445af8` added clearing of effect
payloads/errors and outbox `last_error` for records matched by another
reference, and R1 added structured detection through `audit_log.writes`; the
listed residuals still do not trigger detection by themselves, so operators
must review and handle them manually.
`--operator` defaults to the OS user when omitted. Repeating an already
completed request with no newly matched content prints the coded
`OBJECT_ALREADY_ERASED` no-op report and exits successfully; a repeat with late
content runs the purge again and reports a real erasure. A never-stored id
prints `OBJECT_ERASURE_NOT_FOUND` and exits non-zero.

[Return to the README](../README.md) · [See the full storage API](api-reference.md#stores)
