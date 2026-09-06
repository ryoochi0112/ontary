"""A tiny support-ticket domain ontology, declared entirely via `ontary`'s
public class-based authoring API (spec `typed-authoring.md` AC9) -- the
"second, tiny non-DSO example" proving the engine holds zero DSO
assumptions (spec AC11, T9). Also the README quickstart's code.

Domain: Org -> Queue -> Ticket -> Comment, Agents comment on Tickets. One
action (`EscalateTicket`), one function (`ticketStats`, a `BoundQuery`
aggregate), a two-level `ScopePolicy` (`queue` <- `org`) with `min_n`.

Per spec `typed-actions.md` §6/AC10, `EscalateTicket`/`ticketStats` are
declare-once, class-authored (`@_ontology.action(...)`/`@_ontology
.function(...)`) right here, next to the object/link declarations --
`fixtures.py` now only holds the synthetic-data loader, not any
registration wiring.

`build_ontology()` is called by every test/fixture/connector that needs an
`(Ontology, ObjectStore)` pair. Handlers/functions are declared exactly
once, at import time, on the module-level `_ontology` -- so unlike the
pre-M4b version there is no need to re-create a `FunctionRegistry`/handler
table per call; `build_ontology()` simply hands out the SAME `Ontology`
(registry + declared handlers + `.definition`'s `FunctionRegistry`, all
shared) with a fresh `ObjectStore` each time, so every caller still gets
independent data to seed and tear down. Passing the `Ontology` itself
(not `.definition`) is what lets `OntologyClient`/`build_mcp_server`
auto-bind `EscalateTicket`'s handler with zero manual wiring (spec AC6).
"""

from __future__ import annotations

from typing import Any

from ontary import (
    ActionContext,
    ActionError,
    ActionParams,
    BoundQuery,
    Cardinality,
    DirectProperty,
    EffectHandle,
    EffectPayload,
    LinkHandle,
    ObjectStore,
    Ontology,
    OntologyObject,
    SelfScope,
    Sensitivity,
    ViaLink,
    prop,
    target,
)

LEVELS = ["queue", "org"]
M2O = Cardinality.MANY_TO_ONE

_ontology = Ontology(name="tickets", scope_levels=LEVELS, min_n=2)


@_ontology.object(layer="L0", scope=[SelfScope(level="org")])
class Org(OntologyObject):
    id: str = prop(primary_key=True)
    name: str


@_ontology.object(
    layer="L0",
    scope=[
        SelfScope(level="queue"),
        ViaLink(link_api_name="queueOfOrg", direction="from", parent_type="Org"),
    ],
)
class Queue(OntologyObject):
    id: str = prop(primary_key=True)
    name: str


@_ontology.object(layer="L0", scope="unscoped")
class Agent(OntologyObject):
    id: str = prop(primary_key=True)
    display_name: str
    email: str | None = prop(default=None, sensitivity=Sensitivity(human_visible=False))


@_ontology.object(
    layer="L0",
    owned={"escalated": False},
    scope=[
        DirectProperty(level="queue", property_name="queue_id"),
        ViaLink(link_api_name="ticketInQueue", direction="from", parent_type="Queue"),
    ],
)
class Ticket(OntologyObject):
    id: str = prop(primary_key=True)
    subject: str
    age_hours: float
    status: str | None = None
    queue_id: str | None = prop(default=None, scope_level="queue")
    escalated: bool | None = prop(default=None)


@_ontology.object(
    layer="L0",
    scope=[ViaLink(link_api_name="commentOnTicket", direction="from", parent_type="Ticket")],
)
class Comment(OntologyObject):
    id: str = prop(primary_key=True)
    text: str


queueOfOrg: LinkHandle[Queue, Org] = _ontology.link("queueOfOrg", Queue, Org, M2O)
ticketInQueue: LinkHandle[Ticket, Queue] = _ontology.link("ticketInQueue", Ticket, Queue, M2O)
commentOnTicket: LinkHandle[Comment, Ticket] = _ontology.link("commentOnTicket", Comment, Ticket, M2O)
commentByAgent: LinkHandle[Comment, Agent] = _ontology.link(
    "commentByAgent", Comment, Agent, M2O, identity_revealing=True
)


class TicketEscalationNotification(EffectPayload):
    ticket_id: str
    reason: str | None = None


NOTIFY_TICKET_ESCALATION: EffectHandle[TicketEscalationNotification] = _ontology.effect(
    TicketEscalationNotification,
    api_name="NotifyTicketEscalation",
    description="Notifies the owning queue that a ticket was escalated.",
)


class EscalateTicketParams(ActionParams):
    ticket_id: str = target(Ticket)
    reason: str | None = None


@_ontology.action(
    EscalateTicketParams,
    target=Ticket,
    roles=["Agent", "Manager"],
    display_name="Escalate Ticket",
    description="Escalates a Ticket as urgent.",
    api_name="EscalateTicket",
    effects=[NOTIFY_TICKET_ESCALATION],
)
def _escalate_ticket(ctx: ActionContext, params: EscalateTicketParams) -> dict[str, str]:
    """Same precondition + write as the pre-M4b `fixtures._escalate_ticket_handler`
    (existence check, then a `Ticket.escalated=True` update) -- now via
    `ActionContext` instead of closing over a raw `ObjectStore`."""
    if ctx.read_current("Ticket", params.ticket_id) is None:
        raise ActionError(
            f"ticket {params.ticket_id!r} does not exist",
            code="PRECONDITION_FAILED",
        )
    ctx.update("Ticket", params.ticket_id, {"escalated": True})
    ctx.emit(
        TicketEscalationNotification(
            ticket_id=params.ticket_id,
            reason=params.reason,
        )
    )
    return {"ticket_id": params.ticket_id}


@_ontology.function(
    description="Mean ticket age (hours) for a queue.",
    input_description="A queue_id.",
    output_description="A mean age_hours value.",
    api_name="ticketStats",
)
def _ticket_stats(query: BoundQuery, params: dict[str, Any]) -> float:
    mean = query.aggregate("Ticket", "age_hours", where={"queue_id": params["queue_id"]})
    assert isinstance(mean, float)
    return mean


# Freezes `_ontology` (further `.object()`/`.link()`/`.action()`/
# `.function()` calls would now raise) and validates the assembled
# registry + policy once, at import time.
_ontology.validate()


def build_ontology() -> tuple[Ontology, ObjectStore]:
    """Returns the module-level `Ontology` (registry + declared
    `EscalateTicket`/`ticketStats` handlers, shared and immutable after
    `.validate()` above) paired with a fresh `ObjectStore` -- every caller
    (tests/fixtures/connector) gets independent data to seed and tear
    down, while `OntologyClient(ontology, store, consumer)`/
    `build_mcp_server(ontology, store, consumer)` auto-bind the SAME
    declared handlers with zero manual wiring (spec `typed-actions.md`
    AC6/AC7)."""
    return _ontology, ObjectStore(_ontology.registry)


__all__ = [
    "Org",
    "Queue",
    "Agent",
    "Ticket",
    "Comment",
    "queueOfOrg",
    "ticketInQueue",
    "commentOnTicket",
    "commentByAgent",
    "EscalateTicketParams",
    "TicketEscalationNotification",
    "NOTIFY_TICKET_ESCALATION",
    "build_ontology",
]
