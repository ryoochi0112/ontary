# ontary — the ontology SDK

Author an ontology once — typed objects, links, actions, functions, and a
security policy — and get a governed runtime: scope- and sensitivity-aware
reads, audited business-verb actions, and an MCP server for AI agents. The
engine is domain-agnostic; the ticket domain below is just an example.

Python 3.12+ · pydantic-only core · `mypy --strict` · offline `make verify`.

Release metadata: version
**0.20.0**, store schema **v14**. Releases are annotated tags (`v0.20.0`).

Documentation: **https://ryoochi0112.github.io/ontary/** (English / 日本語).

For AI coding assistants: [`llms.txt`](https://ryoochi0112.github.io/ontary/llms.txt) indexes the docs and [`llms-full.txt`](https://ryoochi0112.github.io/ontary/llms-full.txt) holds every English page in one file.

## Install

From PyPI:

```bash
pip install "ontary[mcp]"
```

`mcp` serves the ontology to AI agents; `postgres` adds `PostgresStore`. The
core depends only on `pydantic`. Pin an exact version: `ontary` is pre-1.0 and
any minor may break you (see [Compatibility](docs/compatibility.md)).

```bash
uv add "ontary[mcp]==0.20.0"
```

or in `pyproject.toml`:

```toml
[project]
dependencies = ["ontary[mcp]==0.20.0"]
```

Without an index, install the tagged git ref:

    uv add "ontary @ git+https://github.com/ryoochi0112/ontary@v0.20.0"

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
    ticket = ctx.get(Ticket, params.ticket_id)
    if ticket is None:
        raise ActionError("ticket does not exist", code="PRECONDITION_FAILED")
    ticket.escalated = True
    ctx.save(ticket)
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

## Command line

The package installs `ontary` and `ontary-mcp`.

- `ontary validate pkg.module:attr` validates an ontology and reports diagnostics. Use `--json` for JSON output.
- `ontary serve pkg.module:attr --dev [--store PATH] [--port N]` serves an ontology over localhost MCP.
- `ontary version` prints the version.

```bash
ontary serve your_app.ontology:ontology --dev --store ./dev.sqlite --port 8000
```

`ontary-mcp` is a placeholder that exits with instructions. Full flags and
exit codes: [CLI reference](docs/cli.md).

## Where to go

| Learn about | Destination |
| --- | --- |
| The docs site (EN / 日本語) | https://ryoochi0112.github.io/ontary/ |
| **Tutorials** | |
| First ontology in ten minutes | [Getting started](docs/getting-started.md) · [日本語](docs/getting-started.ja.md) |
| Model a leave-request approval | [Leave-request tutorial](docs/tutorial-leave-requests.md) |
| **How-to guides** | |
| Testing your ontology | [Testing](docs/testing.md) · [日本語](docs/testing.ja.md) |
| Storage and tenancy | [Storage, tenancy, and schema](docs/storage.md) |
| MCP serving | [MCP serving](docs/mcp-serving.md) |
| Worked recipes | [Tickets reference app](examples/tickets/README.md) · [Maintenance desk](examples/maintenance_desk/README.md) · [Room booking](examples/room_booking/README.md) · [Bill of materials](examples/bill_of_materials/README.md) |
| **Reference** | |
| Names and errors | [API reference](docs/api-reference.md) · [error codes](docs/api-reference.md#error-codes) · [日本語](docs/api-reference.ja.md) |
| API reference pages | [Stores & ingest](docs/api-stores.md) · [日本語](docs/api-stores.ja.md) · [Error codes](docs/api-errors.md) · [日本語](docs/api-errors.ja.md) |
| Command line | [CLI reference](docs/cli.md) · [日本語](docs/cli.ja.md) |
| Compatibility | [Compatibility](docs/compatibility.md) |
| **Explanation** | |
| Authoring an ontology | [Ontology design guide](docs/ontology-design.md) · [日本語](docs/ontology-design.ja.md) |
| Coming from Domain-Driven Design | [Coming from DDD](docs/coming-from-ddd.md) · [日本語](docs/coming-from-ddd.ja.md) |
| **Project** | |
| What is planned | [Roadmap](docs/roadmap.md) |
| Change history | [CHANGELOG.md](CHANGELOG.md) |
| Cutting a release (maintainers) | [Releasing](docs/releasing.md) |
| Reporting a vulnerability | [Security policy](SECURITY.md) |

## License

MIT — see [LICENSE](LICENSE). `ontary` continues the `ontos` SDK, which
Atrae, Inc. authored and released to the author for open-source development.
