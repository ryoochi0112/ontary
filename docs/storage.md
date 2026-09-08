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

The boundary covers objects, links, and audit entries.
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

### Moving across a schema version

There is no in-place upgrade. The supported path is **drop and recreate**: the
engine creates the current schema on an empty store, and the application re-ingests
from its sources. This is the intended shape for an ontary store, which is a
governed projection of source systems, not the system of record; `client.ingest`
and `client.ingest_links` rebuild it. If the store holds facts that exist nowhere
else (Action-written rows, audit history you must keep), export them with the old
ontary version before dropping.

For SQLite, move the old file aside and let the engine create a new one at the
same path.

For PostgreSQL, drop the four engine tables in the schema the store connects to
(the `search_path` of the DSN, or `public`), then construct the store again:

```sql
DROP TABLE IF EXISTS audit_log, links, objects, schema_meta;
```

Indexes and row-level-security policies belong to those tables and go with them.
The next `PostgresStore(...)` sees no `schema_meta`, no `objects`, creates the
schema at the engine's `SCHEMA_VERSION`, re-applies RLS when `rls=True`, and stamps
it. A first deployment on an empty database needs none of this: it creates the
schema on construction.

The stamp is a whole-schema fingerprint, not a release number. It changed from 9
to 10 between the last `ontos` release and `ontary` 0.11.0, and a database stamped
9 is refused by every ontary version. Check the engine's number with
`ontary.store.SCHEMA_VERSION` and the store's with
`SELECT value FROM schema_meta WHERE key = 'schema_version'` before an upgrade so
the re-ingest is planned rather than discovered at startup.

[Return to the README](../README.md) · [See the full storage API](api-reference.md#stores)
