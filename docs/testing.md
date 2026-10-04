# Testing your ontology

[← README](../README.md) · [API reference](api-reference.md)

## Why deterministic tests

Tests should not depend on wall-clock time or random ids. The `ontary.testing` module provides small, deterministic helpers: a given/when/then scenario builder and the low-level helpers underneath it. They need no engine imports and no custom pytest fixtures; failures raise `AssertionError`.

## A first test

First, set up your ontology module. We declare the ontology registry, objects, actions, and validation logic. This module-level setup forms the basis of all our testing scenarios.

```python
from datetime import datetime, timezone
from ontary import (
    ActionContext, ActionError, ActionParams, DirectProperty,
    Ontology, OntologyObject, SelfScope, Source, prop, target,
)
from ontary.testing import (
    FixedClock, SequentialIds, consumer, make_store, raises_code, scenario,
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
    ticket = ctx.get(Ticket, params.ticket_id)
    if ticket is None:
        raise ActionError("ticket does not exist", code="PRECONDITION_FAILED")
    ticket.escalated = True
    ctx.save(ticket)
    return {"ticket_id": params.ticket_id}

ontology.validate()
```

Now we write the first test with the scenario builder. A scenario reads like the business rule it checks: given a starting state, when an actor runs an action, then the outcome holds. Every scenario ends in a `then`. A scenario whose last step is a `when` checks nothing, so finish with `then`, `then_result`, `then_error`, `then_absent`, `then_link`, or `then_no_link`.

`scenario(ontology)` binds a fresh `InMemoryStore`, a `FixedClock` at `2026-01-01T00:00:00Z`, and `SequentialIds("id")` before it seeds anything. `given` takes typed objects, and `when` needs an explicit `by=` consumer.

```python
def test_escalate_success():
    agent = consumer(role="Agent", scope_level="queue", scope_id="queue-a")
    (scenario(ontology)
        .given(
            Queue(id="queue-a", name="Billing"),
            Ticket(id="t-1", subject="Invoice mismatch", queue_id="queue-a"),
        )
        .when(EscalateTicket(ticket_id="t-1"), by=agent)
        .then(Ticket, "t-1", escalated=True))
```

`then(cls, pk, **fields)` compares only the properties you name, against the stored current row. It is not redacted for any consumer. To test redaction, read with `client.get` as that consumer.

A failing check raises `AssertionError` and names the object, the step, and each property that differs:

```text
then: Ticket 't-1' does not match after step 1 (EscalateTicket by Agent):
  escalated: expected False, got True
```

`then_result(expected)` checks the action's return value. `then_absent(cls, pk)` checks that no current row exists, `then_link(handle, from_, to)` and `then_no_link(handle, from_, to)` check links, and `given_link(handle, from_, to)` seeds a link. A scenario may contain several `when` steps, and `then` applies to the last one. A failed step you did not check stops the scenario at the next `when`.

## Asserting error codes

Test failure paths by stable error code, not by message. `then_error(code)` checks that the last action failed with that code. It also proves the failed step changed nothing: current objects and links must equal the state before the step. You do not write that check yourself.

```python
def test_escalate_precondition_failed():
    agent = consumer(role="Agent", scope_level="queue", scope_id="queue-a")
    (scenario(ontology)
        .given(Queue(id="queue-a", name="Billing"))
        .when(EscalateTicket(ticket_id="missing"), by=agent)
        .then_error("PRECONDITION_FAILED"))

def test_viewer_cannot_escalate():
    viewer = consumer(role="Viewer", scope_level="queue", scope_id="queue-a")
    (scenario(ontology)
        .given(
            Queue(id="queue-a", name="Billing"),
            Ticket(id="t-1", subject="Invoice mismatch", queue_id="queue-a"),
        )
        .when(EscalateTicket(ticket_id="t-1"), by=viewer)
        .then_error("PERMISSION_DENIED"))
```

## Overriding the environment

Pass `store=`, `clock=`, `id_factory=`, or `capabilities=` to `scenario()` to replace a default, for example to run the same scenario on SQLite or Postgres. Pass an empty store when overriding. A store that already holds data written under another clock can raise `CLOCK_REGRESSION` or `CLOCK_CONFLICT`. The scenario binds the clock before it seeds, so the order of `given` calls never causes that error.

## The helpers underneath

Scenarios are built on small helpers that you can also use directly. Use them when you need a step that a scenario does not express, such as a raw store write or a bulk ingest.

`make_store(ontology)` creates a fresh empty `InMemoryStore`. `consumer(...)` builds a valid `Consumer` with test-friendly defaults. `raises_code(code)` is a context manager that asserts a block raises an error with that stable code and ignores the message. `FixedClock(start)` returns the same timezone-aware datetime on every call, and `SequentialIds(prefix)` returns `prefix-1`, `prefix-2`, and so on. Both plug into `ontology.bind(...)`.

The same escalation, written with the low-level helpers:

```python
def test_escalate_with_low_level_helpers():
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

    with raises_code("PRECONDITION_FAILED"):
        client.execute(EscalateTicket(ticket_id="missing"))
```

## Fixed time and IDs

Actions generate identifiers and timestamps while they run. Pass a fixed clock and an id factory to `bind` to make both deterministic. The clock is installed on the store, so it also stamps `bulk_upsert`, `bulk_link`, and direct store writes.

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

Bind before you seed. Data written under another clock can make a later update or retire raise `CLOCK_REGRESSION`. This example seeds with `bulk_upsert` after binding, executes an action, and checks that `valid_from` and the audit `ts` equal the fixed instant.

```python
def test_whole_test_is_deterministic():
    from ontary.ingest import bulk_upsert

    t = datetime(2026, 1, 1, tzinfo=timezone.utc)
    store = make_store(ontology)
    runtime = ontology.bind(store, clock=FixedClock(t), id_factory=SequentialIds("t"))
    source = Source(source_system="demo")
    report = bulk_upsert(
        store, ontology.registry, "Ticket",
        [{"id": "t-0", "subject": "Invoice mismatch", "queue_id": "queue-a"}],
        source,
    )
    assert report.errors == []

    agent = consumer(role="Agent", scope_level="queue", scope_id="queue-a")
    runtime.for_consumer(agent).execute(EscalateTicket(ticket_id="t-0"))

    stored = store.read_current("Ticket", "t-0")
    assert stored is not None
    assert stored.lineage.valid_from == t.isoformat(timespec="microseconds")
    assert store.audit_entries()[-1].ts == t
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
