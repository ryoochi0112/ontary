# Governed capabilities and effects

[Back to the README](../README.md) · [API reference](api-reference.md)

Capabilities are declared reads or calls to the outside world. Effects are declared
outside-world writes represented as immutable payload data. Declare both on the
ontology; bind providers and dispatchers to the runtime or consumer that will use
them. The action handler receives only the handles it declared, and an emitted
effect is recorded before it can be dispatched.

Functions may declare capabilities but cannot declare or emit effects. Their
`BoundQuery` has guarded reads and capability providers, without an effect
dispatcher. As with every in-process trust boundary in the SDK, a capability
provider is author code and is not sandboxed by ontary.

## Minimal executable example

This example uses only the current front-door names and can run without an external
service:

<!-- governed-effects-runnable:start -->
```python
from ontary import (
    ActionContext,
    ActionParams,
    Consumer,
    EffectMeta,
    EffectPayload,
    ObjectStore,
    Ontology,
    OntologyObject,
    prop,
)
from ontary import OntologyClient

ontology = Ontology("governed-effects-demo", scope_levels=["org"], min_n=1)


@ontology.object(layer="L0", scope="unscoped", owned=True)
class Incident(OntologyObject):
    id: str = prop(primary_key=True)
    summary: str


class Clock:
    def now(self) -> str:
        return "2026-07-26T00:00:00+00:00"


class IncidentNotification(EffectPayload):
    incident_id: str
    occurred_at: str


CLOCK = ontology.capability(Clock)
NOTIFY = ontology.effect(IncidentNotification)


class OpenIncidentParams(ActionParams):
    incident_id: str
    summary: str


@ontology.action(
    OpenIncidentParams,
    target=Incident,
    roles=["Operator"],
    capabilities=[CLOCK],
    effects=[NOTIFY],
)
def open_incident(
    ctx: ActionContext, params: OpenIncidentParams
) -> dict[str, str]:
    occurred_at = ctx.capability(CLOCK).now()
    ctx.insert(
        "Incident",
        {"id": params.incident_id, "summary": params.summary},
    )
    ctx.emit(
        IncidentNotification(
            incident_id=params.incident_id,
            occurred_at=occurred_at,
        )
    )
    return {"incident_id": params.incident_id}


ontology.validate()
store = ObjectStore(ontology.registry)
delivered: list[IncidentNotification] = []


def dispatch(payload: EffectPayload, _meta: EffectMeta) -> None:
    assert store.read_current("Incident", payload.incident_id) is not None
    assert isinstance(payload, IncidentNotification)
    delivered.append(payload)


consumer = Consumer(
    actor_id="operator-1",
    role="Operator",
    scope_level="org",
    scope_id="org-1",
    kind="human",
)
client = OntologyClient(
    ontology,
    store,
    consumer,
    capabilities={CLOCK: Clock()},
    effects={NOTIFY: dispatch},
)
client.execute(
    OpenIncidentParams(incident_id="incident-1", summary="Example")
)
assert delivered == [
    IncidentNotification(
        incident_id="incident-1",
        occurred_at="2026-07-26T00:00:00+00:00",
    )
]
```
<!-- governed-effects-runnable:end -->

The first dispatch attempt is synchronous, ordered, and in-process. A dispatcher
`Exception` does not roll back committed ontology writes, stop later effects, or
fail `execute()`. `KeyboardInterrupt` and `SystemExit` propagate and stop later
dispatches; committed writes remain committed.

## Durable delivery

Every emitted payload is written to the effect outbox inside the action transaction.
The row and the ontology writes either commit together or disappear together. The
post-commit pass starts delivery, and the embedder can call `client.drain_effects()`
from a worker, scheduled job, or request tail. The SDK starts no background thread.

```python
report = client.drain_effects(limit=100)
for row in client.outbox():
    print(row.effect_id, row.state, row.attempts, row.last_error)
```

Delivery is at-least-once. A leased claim can be recovered after a process crash,
and a process can die after the outside call succeeds but before the row is marked
delivered. A dispatcher must therefore be idempotent on
`EffectMeta.effect_id`; `EffectMeta.attempt` identifies the delivery attempt. A row
that exhausts its retry policy becomes a retained terminal failure rather than being
retried forever.

The delivery-state audit append is best-effort. A pending audit row proves committed
intent and records the work item; it is not proof that an outside system accepted
the payload. Successful capability retrievals are counted as provider accesses,
not as method calls on the unwrapped provider.

## Function audits and the honest boundary

Client-level Function calls have an audit boundary when the Function declares a
capability, unless its declaration overrides that choice. A Function without a
capability cannot reach outside through the runtime, so auditing every such call is
optional. A direct registry invocation has no client boundary.

Every audit entry written by an action invocation carries the same
`invocation_id` as its related outcome and delivery records. Older rows may have no
invocation id because the field is nullable for compatibility; new calls mint one
once per invocation.

The runtime enforces the read-capability and effect-dispatcher contracts only for
the paths it owns. A provider can perform an outward write inline, before an action
fails or outside an effect record. Effects are therefore declared and audited, not
sandboxed or filtered: the SDK does not provide PII filtering, min-N filtering,
destination allowlisting, or containment for trusted in-process code.

See [the API reference side-effects section](api-reference.md#governed-side-effects)
for handle and dispatcher types, and [authority and architecture](authority.md) for
the wider trust-boundary contract.

[Return to the README](../README.md) · [Return to the API reference](api-reference.md)
