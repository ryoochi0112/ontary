# Cookbook

**English** · [API reference](api-reference.md) · [← README](../README.md)

> The Japanese translation of this cookbook is explicitly deferred.

These small recipes are deliberately end-to-end. The Python fences marked
`runnable` are extracted and executed by `tests/test_docs.py`, so keep their
assertions and imports honest when adapting them to an application.

## 1. Add a scoped type

Use a `DirectProperty` scope rule when a row carries the id of the scope that
owns it. The class authoring facade assembles that declaration into a
`ScopePolicy`; every consumer-bound read then applies it.

<!-- cookbook-scoped-type-runnable:start -->
```python
from ontary import (
    Consumer,
    DirectProperty,
    ObjectStore,
    Ontology,
    OntologyObject,
    ScopePolicy,
    Source,
    prop,
)

ontology = Ontology(name="scoped-invoices", scope_levels=["org"])


@ontology.object(
    layer="L0",
    scope=[DirectProperty(level="org", property_name="org_id")],
)
class Invoice(OntologyObject):
    id: str = prop(primary_key=True)
    org_id: str = prop(scope_level="org")
    total_amount: int


ontology.validate()
assert isinstance(ontology.definition.policy, ScopePolicy)
store = ObjectStore(ontology.registry)
store.insert(
    "Invoice",
    {"id": "invoice-1", "org_id": "org-a", "total_amount": 125},
    Source(source_system="cookbook"),
)

org_a = Consumer(
    actor_id="agent-a",
    role="Member",
    scope_level="org",
    scope_id="org-a",
    kind="human",
)
org_b = Consumer(
    actor_id="agent-b",
    role="Member",
    scope_level="org",
    scope_id="org-b",
    kind="human",
)

org_a_client = ontology.bind(store).for_consumer(org_a)
org_b_client = ontology.bind(store).for_consumer(org_b)
assert org_a_client.get(Invoice, "invoice-1") is not None
assert org_b_client.list(Invoice, limit=None) == []
```
<!-- cookbook-scoped-type-runnable:end -->

Diagnostics are intentionally advisory. When `ontology.diagnose()` reports
name heuristics, they use `severity="warn"`, so a legitimate stored fact called
`total_amount` can fire `STORED_DERIVABLE`, and a legitimate business action
called `CreateInvoice` can fire `CRUD_ACTION_NAME` because `Create*` resembles
CRUD. Read the fix hint and keep the declaration when its domain meaning is
correct; warnings are not automatic validation failures.

## 5. Serve MCP in development

The Python setup below is doc-tested. The shell command is intentionally not
executed by pytest because it starts a blocking HTTP server and requires the
optional MCP dependency.

<!-- cookbook-serve-dev-runnable:start -->
```python
from ontary import Ontology, OntologyObject, prop

ontology = Ontology(name="dev-server", scope_levels=["org"])


@ontology.object(layer="L0", scope="unscoped", owned=True)
class HealthCheck(OntologyObject):
    id: str = prop(primary_key=True)
    status: str


ontology.validate()
assert ontology.definition.policy.unscoped_types == {"HealthCheck"}
```
<!-- cookbook-serve-dev-runnable:end -->

Put that ontology in an importable module, then run:

```bash
ontary serve your_app.ontology:ontology --dev --store ./dev.sqlite --port 8000
```

`ontary serve --dev` binds to `127.0.0.1` only. Its labeled development
consumer is fail-closed: it sees unscoped rows only, and it cannot execute
actions on scoped ontologies. If a scoped row is hidden while you exercise a
local server, review the type's `ScopePolicy` declaration rather than weakening
it — see [Scope policy](api-reference.md#scope-policy).
