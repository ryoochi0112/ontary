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

## 2. Test an action with `ontary.testing`

`ontary.testing` supplies deterministic seams without requiring test-framework
fixtures. `capture_effects()` records the emitted `(payload, meta)` pair instead
of sending it to an outside system, while `FixedClock` and `SequentialIds`
make the audit and effect metadata repeatable.

<!-- cookbook-testing-action-runnable:start -->
```python
from datetime import datetime, timezone

from ontary import (
    ActionContext,
    ActionError,
    ActionParams,
    EffectPayload,
    Ontology,
    OntologyObject,
    Source,
    prop,
)
from ontary.testing import (
    FixedClock,
    SequentialIds,
    capture_effects,
    consumer,
    make_store,
)

ontology = Ontology(name="action-tests", scope_levels=["org"], min_n=1)


@ontology.object(layer="L0", scope="unscoped", owned=True)
class Order(OntologyObject):
    id: str = prop(primary_key=True)


class ShipmentNotice(EffectPayload):
    order_id: str


notice = ontology.effect(ShipmentNotice)


class ShipOrder(ActionParams):
    order_id: str


@ontology.action(
    ShipOrder,
    target=Order,
    roles=["Operator"],
    effects=[notice],
    api_name="ShipOrder",
)
def ship_order(ctx: ActionContext, params: ShipOrder) -> dict[str, str]:
    if ctx.read_current("Order", params.order_id) is None:
        raise ActionError("order does not exist", code="PRECONDITION_FAILED")
    ctx.emit(ShipmentNotice(order_id=params.order_id))
    return {"order_id": params.order_id, "status": "shipped"}


store = make_store(ontology)
store.insert("Order", {"id": "order-1"}, Source(source_system="cookbook"))
captured = capture_effects()
fixed = datetime(2099, 1, 1, 12, 0, tzinfo=timezone.utc)
runtime = ontology.bind(
    store,
    clock=FixedClock(fixed),
    id_factory=SequentialIds("action"),
    effects={notice: captured},
)
client = runtime.for_consumer(consumer(role="Operator"))

assert client.execute(ShipOrder(order_id="order-1")) == {
    "order_id": "order-1",
    "status": "shipped",
}
assert len(captured.effects) == 1
payload, meta = captured.effects[0]
assert payload.order_id == "order-1"
assert meta.action == "ShipOrder"
assert meta.effect_id == "action-2"
assert meta.ts == fixed
assert store.audit_entries()[0].invocation_id == "action-1"
```
<!-- cookbook-testing-action-runnable:end -->

## 3. Evolve a type with an upcaster

Version the same object type when its shape changes. The upcaster below turns a
stored v1 `label` into the v2 `name`; `upcast_object_type` then permanently
rewrites the rows through that declared chain. The store can open under the
versioned declaration without `accept_ontology_drift`.

<!-- cookbook-type-evolution-runnable:start -->
```python
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from ontary import ObjectStore, Ontology, OntologyObject, Source, prop
from ontary.migrate import upcast_object_type


old = Ontology(name="evolution", scope_levels=["org"])


@old.object(layer="L0", api_name="Widget", scope="unscoped", version=1)
class WidgetV1(OntologyObject):
    id: str = prop(primary_key=True)
    label: str


old.validate()

with TemporaryDirectory() as directory:
    path = str(Path(directory) / "evolution.sqlite")
    old_store = ObjectStore(old.registry, path)
    old_store.insert(
        "Widget",
        {"id": "widget-1", "label": "legacy"},
        Source(source_system="cookbook"),
    )

    current = Ontology(name="evolution", scope_levels=["org"])

    @current.object(
        layer="L0", api_name="Widget", scope="unscoped", version=2
    )
    class WidgetV2(OntologyObject):
        id: str = prop(primary_key=True)
        name: str | None = prop(default=None)

    @current.upcaster(WidgetV2, from_version=1)
    def widget_v1_to_v2(payload: dict[str, Any]) -> dict[str, Any]:
        payload["name"] = payload.pop("label")
        return payload

    current.validate()
    store = ObjectStore(current.registry, path)
    assert store.read_current("Widget", "widget-1").payload["name"] == "legacy"

    report = upcast_object_type(store, current.registry, "Widget")
    assert report.ok and report.scanned == 1 and report.changed == 1
    assert store.read_current("Widget", "widget-1").payload == {
        "id": "widget-1",
        "name": "legacy",
    }
    assert store.audit_entries()[-1].kind == "migration"
```
<!-- cookbook-type-evolution-runnable:end -->

The upcaster is a read-time compatibility bridge until this migration runs. It
must remain registered while any old row still needs it; after the report has
rewritten every row at the current version, it can become dead code for that
type.

## 4. Debug a hidden row with explain

Explain is an operator-only oracle. A consumer should continue to receive the
ordinary hidden-row result, while an operator with access to the runtime can
call `OntologyRuntime.explain_read` and inspect the decision trace, including
the resolved scope path and refusal code.
`DecisionTrace` is deliberately imported from `ontary.explain`; it is not a
front-door consumer API and is not served through MCP.

<!-- cookbook-explain-hidden-row-runnable:start -->
```python
from ontary import (
    Consumer,
    DirectProperty,
    ObjectStore,
    Ontology,
    OntologyObject,
    Source,
    prop,
)
from ontary.explain import DecisionTrace
from ontary.testing import raises_code

ontology = Ontology(name="explain-demo", scope_levels=["org"])


@ontology.object(
    layer="L0",
    scope=[DirectProperty(level="org", property_name="org_id")],
)
class Document(OntologyObject):
    id: str = prop(primary_key=True)
    org_id: str = prop(scope_level="org")
    title: str


ontology.validate()
store = ObjectStore(ontology.registry)
store.insert(
    "Document",
    {"id": "doc-1", "org_id": "org-a", "title": "Private"},
    Source(source_system="cookbook"),
)
runtime = ontology.bind(store)
consumer = Consumer(
    actor_id="reader-b",
    role="Member",
    scope_level="org",
    scope_id="org-b",
    kind="human",
)

with raises_code("VISIBILITY_DENIED"):
    runtime.for_consumer(consumer).get(Document, "doc-1")
trace = runtime.explain_read(consumer, "Document", "doc-1")
assert isinstance(trace, DecisionTrace)
assert trace.verdict == "denied"
assert trace.error_code == "VISIBILITY_DENIED"
assert trace.rules[0].resolved_scope_id == "org-a"
```
<!-- cookbook-explain-hidden-row-runnable:end -->

Do not add `explain_read` to an `OntologyClient` wrapper or MCP tool: the
operator trace can reveal that a hidden row exists. For command-line diagnosis,
use the corresponding `ontary explain` command with an explicitly authorized
operator consumer.

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
local server, debug the decision with `ontary explain` rather than weakening the
scope declaration, for example:

```bash
ontary explain your_app.ontology:ontology \
  --consumer Member:org:org-a \
  --read Document:doc-1 \
  --store ./dev.sqlite
```
