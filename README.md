# ontary — a domain-agnostic ontology SDK

Declare an ontology once — typed objects, links, actions, functions, and a
security policy — and get a governed runtime: scope- and sensitivity-aware
reads, audited business-verb actions, and an MCP server for AI agents.
The engine stays domain-agnostic; the tickets example is just one small domain.

Python 3.12+ · pydantic-only core · `mypy --strict` · offline `make verify`.

Release metadata: version
**0.8.0**, store schema **v9**. Releases are annotated tags (`v0.8.0`).

## Install

    uv add "ontary @ git+https://github.com/ryoochi0112/ontary@v0.8.0"

## Quickstart

This short runnable path uses a support-ticket domain. The complete
`Org → Queue → Ticket → Comment` ontology, connectors, effects, functions, and
runnable MCP stdio and effects-drain examples are in
[`examples/tickets/`](examples/tickets/).

<!-- quickstart-runnable:start -->
```python
from ontary import (
    ActionContext, ActionError, ActionParams, Consumer, DirectProperty,
    ObjectStore, Ontology, OntologyObject, Source, prop, target,
)
ontology = Ontology(name="tickets", scope_levels=["queue"])
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
store = ObjectStore(ontology.registry)
ticket_id = store.insert(
    "Ticket", {"subject": "Invoice mismatch", "queue_id": "queue-a"},
    Source(source_system="demo"),
)
client = ontology.bind(store).for_consumer(
    Consumer(actor_id="agent-1", role="Agent", scope_level="queue",
             scope_id="queue-a", kind="human")
)
ticket = client.get(Ticket, ticket_id)
assert ticket is not None and ticket.subject == "Invoice mismatch"
client.execute(EscalateTicket(ticket_id=ticket_id))
assert client.get(Ticket, ticket_id).escalated is True
```
<!-- quickstart-runnable:end -->

## Where to go

| Learn about | Destination |
| --- | --- |
| Authoring an ontology | [Ontology design guide](docs/ontology-design.md) |
| オントロジー設計 | [日本語ガイド](docs/ontology-design.ja.md) |
| Look up names and errors | [API reference](docs/api-reference.md) · [error-code table](docs/api-reference.md#error-codes) |
| API リファレンス | [日本語リファレンス](docs/api-reference.ja.md) |
| Storage and tenancy | [Storage, tenancy, and schema](docs/storage.md) |
| Connectors | [Connectors and canonical staging](docs/connectors.md) |
| MCP serving | [MCP serving](docs/mcp-serving.md) |
| Governed effects | [Governed capabilities and effects](docs/effects.md) |
| Queries and pagination | [Queries, typed reads, and pagination](docs/queries.md) |
| Authority and architecture | [Authority, declarations, and architecture](docs/authority.md) |
| Cookbook recipes | [Cookbook](docs/cookbook.md) |
| Compatibility and migration | [CHANGELOG.md](CHANGELOG.md) · [Compatibility](docs/compatibility.md) |
## License

MIT — see [LICENSE](LICENSE).
