# Testing your ontology

[← README](../README.md) · [API reference](api-reference.md)

## Why deterministic tests

Tests should not depend on wall-clock time or random ids. The `ontary.testing` module provides small, deterministic helpers that compose only the public `ontary` API. These utilities require no engine internals or custom pytest fixtures to run.

## The helpers

The module exposes key functions and classes to isolate and control your testing environment. You can instantiate fresh in-memory storage, mock user agents, mock the clock, and control id generation. These primitives ensure your assertions remain stable across different environments.

The `make_store` helper creates a fresh empty in-memory store for the ontology's registry. The `consumer` helper generates a mock user with common defaults for permissions and scope level. The `raises_code` context manager asserts that a code block raises an expected stable exception code.

The `FixedClock` helper returns the same timezone-aware datetime on every call. The `SequentialIds` utility generates predictable ids using a custom prefix. Both helpers plug directly into your ontology client binding.

## A first test

First, set up your ontology module. We declare the ontology registry, objects, actions, and validation logic. This module-level setup forms the basis of all our testing scenarios.

```python
from datetime import datetime, timezone
from ontary import (
    ActionContext, ActionError, ActionParams, DirectProperty,
    Ontology, OntologyObject, SelfScope, Source, prop, target,
)
from ontary.testing import (
    FixedClock, SequentialIds, consumer, make_store, raises_code,
)

ontology = Ontology(name="tickets", scope_levels=["queue"])

@ontology.object(layer="L0", scope=[SelfScope(level="queue")])
class Queue(OntologyObject):
    id: str = prop(primary_key=True)
    name: str

@ontology.object(
    layer="L0", owned={"escalated": False},
    scope=[DirectProperty(level="queue", property_name="queue_id")],
)
class Ticket(OntologyObject):
    id: str = prop(primary_key=True)
    subject: str
    queue_id: str = prop(scope_level="queue")
    escalated: bool | None = prop(default=False)

class EscalateTicket(ActionParams):
    ticket_id: str = target(Ticket)

@ontology.action(
    EscalateTicket, target=Ticket, roles=["Agent"],
    display_name="Escalate ticket", description="Mark a ticket urgent.",
    api_name="EscalateTicket",
)
def escalate(ctx: ActionContext, params: EscalateTicket) -> dict[str, str]:
    if ctx.read_current("Ticket", params.ticket_id) is None:
        raise ActionError("ticket does not exist", code="PRECONDITION_FAILED")
    ctx.update("Ticket", params.ticket_id, {"escalated": True})
    return {"ticket_id": params.ticket_id}

ontology.validate()
```

Now, we write our first test function to verify successful escalation. We initialize an empty in-memory store using our ontology. Then, we insert test data and execute our action.

```python
def test_escalate_success():
    store = make_store(ontology)
    source = Source(source_system="demo")
    store.insert("Queue", {"id": "queue-a", "name": "Billing"}, source)
    ticket_id = store.insert(
        "Ticket", {"subject": "Invoice mismatch", "queue_id": "queue-a"}, source
    )
    agent = consumer(role="Agent", scope_level="queue", scope_id="queue-a")
    client = ontology.bind(store).for_consumer(agent)
    client.execute(EscalateTicket(ticket_id=ticket_id))
    assert client.get(Ticket, ticket_id).escalated is True
```

## Asserting error codes

We must test failure paths by asserting specific stable error codes. The `raises_code` helper checks that a block raises the expected error. This guarantees stable tests that ignore changing prose messages.

```python
def test_escalate_errors():
    store = make_store(ontology)
    source = Source(source_system="demo")
    ticket_id = store.insert(
        "Ticket", {"subject": "Invoice mismatch", "queue_id": "queue-a"}, source
    )

    agent = consumer(role="Agent", scope_level="queue", scope_id="queue-a")
    client = ontology.bind(store).for_consumer(agent)
    with raises_code("PRECONDITION_FAILED"):
        client.execute(EscalateTicket(ticket_id="missing"))

    viewer = consumer(role="Viewer", scope_level="queue", scope_id="queue-a")
    viewer_client = ontology.bind(store).for_consumer(viewer)
    with raises_code("PERMISSION_DENIED"):
        viewer_client.execute(EscalateTicket(ticket_id=ticket_id))

    assert viewer_client.get(Ticket, ticket_id).escalated is False
```

## Fixed time and IDs

Actions often generate identifiers and timestamps during execution. We can mock these values to ensure deterministic test runs. Passing a fixed clock and id factory to the bind call overrides default dynamic behaviors.

```python
def test_fixed_time_and_ids():
    clock = FixedClock(datetime(2026, 1, 1, tzinfo=timezone.utc))
    id_factory = SequentialIds("t")
    assert clock() == datetime(2026, 1, 1, tzinfo=timezone.utc)
    assert id_factory() == "t-1"

    store = make_store(ontology)
    runtime = ontology.bind(store, clock=clock, id_factory=id_factory)
    assert runtime is not None
```

## Diagnostics as a test

Ontology diagnostics help identify architectural and design issues. We can integrate these checks into our test suite. This test fails only on `error` findings; advisory warnings pass.

```python
def test_diagnostics():
    errors = [f for f in ontology.diagnose() if f.severity == "error"]
    assert errors == []
```

## Related pages

Learn more about setting up and running your ontology applications. Review the CLI tool instructions to validate your schemas during development. You can also explore real-world examples.

- [Getting started](getting-started.md)
- [API reference](api-reference.md)
- [Command-line interface](cli.md)
- [Tickets example](../examples/tickets/README.md)
