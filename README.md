# ontary — the ontology SDK

Author an ontology once — typed objects, links, actions, functions, and a
security policy — and get a governed runtime: scope- and sensitivity-aware
reads, audited business-verb actions, and an MCP server for AI agents. The
engine is domain-agnostic; the ticket domain below is just an example.

Python 3.12+ · pydantic-only core · `mypy --strict` · offline `make verify`.

Release metadata: version
**0.10.0**, store schema **v10**. Releases are annotated tags (`v0.10.0`).

Documentation: **https://ryoochi0112.github.io/ontary/** (English / 日本語).

## Install

From PyPI:

```bash
pip install "ontary[mcp]"
```

`mcp` serves the ontology to AI agents; `postgres` adds `PostgresStore`. The
core depends only on `pydantic`. Pin an exact version: `ontary` is pre-1.0 and
any minor may break you (see [Compatibility](docs/compatibility.md)).

```bash
uv add "ontary[mcp]==0.10.0"
```

or in `pyproject.toml`:

```toml
[project]
dependencies = ["ontary[mcp]==0.10.0"]
```

Without an index, install the tagged git ref:

    uv add "ontary @ git+https://github.com/ryoochi0112/ontary@v0.10.0"

## Quickstart

Two object types, one business-verb action, a SQLite store, and an MCP server
for one queue-scoped agent. Runs as-is (`tests/test_docs.py` executes it).

<!-- quickstart-runnable:start -->
```python
from ontary import (
    ActionContext, ActionError, ActionParams, Consumer, DirectProperty,
    ObjectStore, Ontology, OntologyObject, SelfScope, Source,
    build_mcp_server, prop, target,
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
store = ObjectStore(ontology.registry)          # ObjectStore(registry, "tickets.db") persists
source = Source(source_system="demo")
store.insert("Queue", {"id": "queue-a", "name": "Billing"}, source)
ticket_id = store.insert(
    "Ticket", {"subject": "Invoice mismatch", "queue_id": "queue-a"}, source
)
agent = Consumer(actor_id="agent-1", role="Agent", scope_level="queue",
                 scope_id="queue-a", kind="human")
client = ontology.bind(store).for_consumer(agent)
client.execute(EscalateTicket(ticket_id=ticket_id))
assert client.get(Ticket, ticket_id).escalated is True

server = build_mcp_server(ontology, store, agent)   # server.run() serves stdio
```
<!-- quickstart-runnable:end -->

The full `Org → Queue → Ticket → Comment` ontology with functions and a
multi-consumer MCP server is [`examples/tickets/`](examples/tickets/).

## Where to go

| Learn about | Destination |
| --- | --- |
| The docs site (EN / 日本語) | https://ryoochi0112.github.io/ontary/ |
| Authoring an ontology | [Ontology design guide](docs/ontology-design.md) · [日本語](docs/ontology-design.ja.md) |
| Names and errors | [API reference](docs/api-reference.md) · [error codes](docs/api-reference.md#error-codes) · [日本語](docs/api-reference.ja.md) |
| Storage and tenancy | [Storage, tenancy, and schema](docs/storage.md) |
| MCP serving | [MCP serving](docs/mcp-serving.md) |
| Worked recipes | [Tickets reference app](examples/tickets/README.md) |
| Compatibility and migration | [CHANGELOG.md](CHANGELOG.md) · [Compatibility](docs/compatibility.md) |
| Cutting a release (maintainers) | [Releasing](docs/releasing.md) |

## License

MIT — see [LICENSE](LICENSE). `ontary` continues the `ontos` SDK, which
Atrae, Inc. authored and released to the author for open-source development.
