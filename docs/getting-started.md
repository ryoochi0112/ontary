# Getting Started

[← README](../README.md) · [API reference](api-reference.md)

This tutorial helps you build your first working ontology in ten minutes. You will define objects, actions, and functions.

## Install

Install the package with Model Context Protocol support. Python 3.12 or newer is required.

```bash
pip install "ontary[mcp]"
```

The core library depends only on `pydantic`. The `postgres` extra adds `PostgresStore` if needed. Always pin an exact version, as `ontary` is pre-1.0. Minor releases can introduce breaking changes.

## Declare object types

Create a file named `app.py`. First, declare the ontology and its object types.

```python
from ontary import DirectProperty, Ontology, OntologyObject, SelfScope, prop

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
```

The `Ontology` manages types and validation. Objects on the `L0` layer use scope rules to secure access.

## Declare an action

Actions represent business verbs. They capture state transitions rather than generic CRUD setters.

```python
from ontary import ActionContext, ActionError, ActionParams, target

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
```

This action targets the `Ticket` object. It verifies existence and updates the status safely.

## Add a derived-value function

Functions calculate values without modifying state. They lack direct database write access.

```python
from typing import Any
from ontary import BoundQuery

@ontology.function(
    description="Total tickets in a queue.",
    input_description="A queue_id.",
    output_description="Count of tickets.",
    api_name="ticketCount",
)
def ticket_count(query: BoundQuery, params: dict[str, Any]) -> int:
    return query.count("Ticket", where={"queue_id": params["queue_id"]})
```

You must declare all functions before calling validate. This function counts the tickets in a queue.

## Validate and diagnose

The `validate` method freezes the ontology definition. Any subsequent attempts to declare objects or actions will fail.

```python
ontology.validate()

for f in ontology.diagnose():
    print(f.severity, f.code, f.location, f.fix_hint)
```

Diagnostics run advisory checks. Heuristics like `STORED_DERIVABLE` or `CRUD_ACTION_NAME` return warnings. These warnings do not stop execution.

## Bind a store and a consumer

Create an in-memory store and seed it with data. Define a consumer to apply security policies.

```python
from ontary import Consumer, ObjectStore, Source

store = ObjectStore(ontology.registry)
source = Source(source_system="demo")

store.insert("Queue", {"id": "queue-a", "name": "Billing"}, source)
ticket_id = store.insert(
    "Ticket", {"subject": "Invoice mismatch", "queue_id": "queue-a"}, source
)

agent = Consumer(
    actor_id="agent-1", role="Agent", scope_level="queue",
    scope_id="queue-a", kind="human"
)
client = ontology.bind(store).for_consumer(agent)
```

Use `ObjectStore(registry, "tickets.db")` to persist data to disk. The `OntologyClient` handles secure reads.

## Execute the action and read back

Use the client to run actions and query properties.

```python
client.execute(EscalateTicket(ticket_id=ticket_id))
assert client.get(Ticket, ticket_id).escalated is True
assert client.call_function("ticketCount", {"queue_id": "queue-a"}) == 1
```

The client applies scope, sensitivity, and row-visibility rules. You can also use `list`, `traverse`, and `call_function`.

## Serve to an AI agent

You can expose the ontology over the Model Context Protocol. Stdio serves as the default transport.

```python
from ontary import build_mcp_server

server = build_mcp_server(ontology, store, agent)
```

To run a development server on port 8000, use the command-line interface. It binds only to `127.0.0.1`.

```bash
ontary serve app:ontology --dev --store ./dev.sqlite --port 8000
```

Validate your file using the CLI tool. Exit code 0 indicates success or warnings only.

```bash
ontary validate app:ontology
```

## The whole program

Here is the complete runnable code for `app.py`.

```python
from typing import Any
from ontary import (
    ActionContext, ActionError, ActionParams, BoundQuery, Consumer,
    DirectProperty, ObjectStore, Ontology, OntologyObject, SelfScope,
    Source, build_mcp_server, prop, target,
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

@ontology.function(
    description="Total tickets in a queue.",
    input_description="A queue_id.",
    output_description="Count of tickets.",
    api_name="ticketCount",
)
def ticket_count(query: BoundQuery, params: dict[str, Any]) -> int:
    return query.count("Ticket", where={"queue_id": params["queue_id"]})

ontology.validate()

for f in ontology.diagnose():
    print(f.severity, f.code, f.location, f.fix_hint)

store = ObjectStore(ontology.registry)
source = Source(source_system="demo")

store.insert("Queue", {"id": "queue-a", "name": "Billing"}, source)
ticket_id = store.insert(
    "Ticket", {"subject": "Invoice mismatch", "queue_id": "queue-a"}, source
)

agent = Consumer(
    actor_id="agent-1", role="Agent", scope_level="queue",
    scope_id="queue-a", kind="human"
)

client = ontology.bind(store).for_consumer(agent)
client.execute(EscalateTicket(ticket_id=ticket_id))

assert client.get(Ticket, ticket_id).escalated is True
assert client.call_function("ticketCount", {"queue_id": "queue-a"}) == 1

server = build_mcp_server(ontology, store, agent)
```

## Next steps

For deep architectural concepts and security rules, see the following topics:

* Learn design rules in [Ontology Design](ontology-design.md). Store facts once and declare security with `ScopePolicy`, `Sensitivity`, and min-N.
* Review all classes and types in the [API Reference](api-reference.md).
* Read about storage options in [Storage](storage.md).
* Explore agent integration details in [MCP Serving](mcp-serving.md).
* Learn about command-line tools in the [CLI Reference](cli.md).
* Find testing guidelines in [Testing](testing.md).
* See a fully realized example in the [Tickets Example](../examples/tickets/README.md).
