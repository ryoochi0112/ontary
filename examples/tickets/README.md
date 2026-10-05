# Tickets reference app

`examples/tickets/` is the in-repo reference consumer for `ontary`. It models
a small support-ticket workflow:

- **Org** (`Org`) and **Queue** (`Queue`) define the organization and queue
  scopes.
- **Agent** (`Agent`) represents a support agent.
- **Ticket** (`Ticket`) is a source-backed ticket with a `channel` choices
  property (`email`, `chat`, or `phone`).
- **Comment** (`Comment`) records a comment on a ticket.
- **Escalation** (`Escalation`) is an owned lifecycle object linked to a ticket
  and assigned to an agent.

The actions are `EscalateTicket`, `ResolveEscalation`, and
`ArchiveEscalation`. The Functions are `ticketStats` and
`isTicketEscalated`. “Escalated” is derived from open Escalation links; it is
not stored on Ticket.

See [`ontology.py`](ontology.py) for the full declaration.

## Running it

From the repository root, [`run_mcp.py`](run_mcp.py) starts a multi-consumer
MCP server over the tickets ontology:

```bash
uv run python -m examples.tickets.run_mcp
```

Note: `run_mcp.py`'s `build_multi_consumer_server()` omits `token_verifier`
and `auth` for brevity. Unlike [`docs/mcp-serving.md`](../../docs/mcp-serving.md),
copying it verbatim over a real transport yields `UNAUTHENTICATED` calls.

## Recipes

These small recipes are deliberately end-to-end. The Python fences marked
`runnable` are extracted and executed by `tests/test_docs.py`, so keep their
assertions and imports honest when adapting them to an application.

### 1. Add a scoped type

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
    amount: int


ontology.validate()
assert isinstance(ontology.definition.policy, ScopePolicy)
store = ObjectStore(ontology.registry)
store.insert(
    "Invoice",
    {"id": "invoice-1", "org_id": "org-a", "amount": 125},
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
`total_amount` can fire `STORED_DERIVABLE` (the recipe above names it `amount`), and a legitimate business action
called `CreateInvoice` can fire `CRUD_ACTION_NAME` because `Create*` resembles
CRUD. Read the fix hint. When the declaration's domain meaning is correct, accept
the finding where you declare it with `accept=`, for example
`prop(accept="STORED_DERIVABLE")` or
`@ontology.action(..., accept="CRUD_ACTION_NAME")`. Warnings are not automatic
validation failures; `ontary validate --strict` is what turns a remaining warning
into a failing exit code.

### 2. Serve MCP in development

The Python setup below is doc-tested. The shell command is intentionally not
executed by pytest because it starts a blocking HTTP server and requires the
optional MCP dependency.

<!-- cookbook-serve-dev-runnable:start -->
```python
from typing import Literal

from ontary import Ontology, OntologyObject, prop

ontology = Ontology(name="dev-server", scope_levels=["org"])


@ontology.object(layer="L0", scope="unscoped", owned=True)
class HealthCheck(OntologyObject):
    id: str = prop(primary_key=True)
    status: Literal["ok", "degraded", "down"]


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
it — see [Scope policy](../../docs/api-reference.md#scope-policy).

### 3. Test an action as given / when / then

`ontary.testing.scenario` states a test in business terms: the starting state as
typed objects, the action and who runs it, and the expected state. It binds a
deterministic store, clock, and id factory before it seeds anything.

<!-- cookbook-given-when-then-runnable:start -->
```python
from ontary import (
    ActionContext,
    ActionError,
    ActionParams,
    DirectProperty,
    Ontology,
    OntologyObject,
    SelfScope,
    prop,
    target,
)
from ontary.testing import consumer, scenario

ontology = Ontology(name="tickets-recipe", scope_levels=["queue"])


@ontology.object(layer="L0", scope=[SelfScope(level="queue")])
class Queue(OntologyObject):
    id: str = prop(primary_key=True)
    name: str


@ontology.object(
    layer="L0",
    owned={"escalated": False},
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
    EscalateTicket,
    target=Ticket,
    roles=["Agent"],
    display_name="Escalate ticket",
    description="Mark a ticket urgent.",
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
agent = consumer(role="Agent", scope_level="queue", scope_id="queue-a")
viewer = consumer(role="Viewer", scope_level="queue", scope_id="queue-a")
queue = Queue(id="queue-a", name="Billing")
ticket = Ticket(id="t-1", subject="Invoice mismatch", queue_id="queue-a")

(
    scenario(ontology)
    .given(queue, ticket)
    .when(EscalateTicket(ticket_id="t-1"), by=agent)
    .then_result({"ticket_id": "t-1"})
    .then(Ticket, "t-1", escalated=True)
)

(
    scenario(ontology)
    .given(queue, ticket)
    .when(EscalateTicket(ticket_id="t-1"), by=viewer)
    .then_error("PERMISSION_DENIED")  # also proves the ticket is unchanged
)
```
<!-- cookbook-given-when-then-runnable:end -->

Every scenario ends in a `then*` call; a scenario that ends in a `when` checks
nothing. See [`docs/testing.md`](../../docs/testing.md) for the full helper set.

## Test coverage

- [`tests/test_examples_tickets_e2e.py`](../../tests/test_examples_tickets_e2e.py)
  exercises the app end to end: tenant isolation, `ontary.testing` adoption,
  the multi-consumer MCP example with two queue-scoped identities, all three
  CLI commands (`validate`, `serve`, `version`), and a clean `diagnose()`
  result.
- [`tests/test_examples_smoke.py`](../../tests/test_examples_smoke.py) smoke-
  tests the example scripts.
