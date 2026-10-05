"""A support-ticket ontology with source-backed tickets and owned escalations.

Org and Queue scope Tickets, Comments, and Escalations. Agents escalate tickets,
resolve escalations, and Managers archive them. Escalation status is derived by
`isTicketEscalated`; `ticketStats` aggregates ticket ages under the scope policy.

Handlers are declared once on the module-level ontology. `build_ontology()`
pairs that validated ontology with a fresh store for independent data.
"""

from __future__ import annotations

from typing import Literal

from ontary import (
    ActionContext,
    ActionError,
    ActionParams,
    BoundQuery,
    Cardinality,
    DirectProperty,
    FunctionParams,
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
from ontary.meta import TransitionDef

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
    scope=[
        DirectProperty(level="queue", property_name="queue_id"),
        ViaLink(link_api_name="ticketInQueue", direction="from", parent_type="Queue"),
    ],
)
class Ticket(OntologyObject):
    id: str = prop(primary_key=True)
    subject: str
    age_hours: float
    status: Literal["open", "pending", "closed"] | None = None
    queue_id: str | None = prop(default=None, scope_level="queue")
    channel: str | None = prop(default=None, choices=["email", "chat", "phone"])


@_ontology.object(
    layer="L0",
    scope=[ViaLink(link_api_name="commentOnTicket", direction="from", parent_type="Ticket")],
)
class Comment(OntologyObject):
    id: str = prop(primary_key=True)
    text: str


@_ontology.object(
    layer="L0",
    owned=True,
    scope=[
        ViaLink(
            link_api_name="escalationOnTicket",
            direction="from",
            parent_type="Ticket",
        )
    ],
)
class Escalation(OntologyObject):
    id: str = prop(primary_key=True)
    reason: str | None = None
    state: Literal["open", "resolved"] = prop(transitions=TransitionDef(
        initial=("open",),
        moves={"open": ("resolved",), "resolved": ()},
    ))


queueOfOrg: LinkHandle[Queue, Org] = _ontology.link("queueOfOrg", Queue, Org, M2O)
ticketInQueue: LinkHandle[Ticket, Queue] = _ontology.link("ticketInQueue", Ticket, Queue, M2O)
commentOnTicket: LinkHandle[Comment, Ticket] = _ontology.link("commentOnTicket", Comment, Ticket, M2O)
commentByAgent: LinkHandle[Comment, Agent] = _ontology.link(
    "commentByAgent", Comment, Agent, M2O, identity_revealing=True
)
escalationOnTicket: LinkHandle[Escalation, Ticket] = _ontology.link(
    "escalationOnTicket", Escalation, Ticket, M2O, owned=True
)
escalationAssignedTo: LinkHandle[Escalation, Agent] = _ontology.link(
    "escalationAssignedTo", Escalation, Agent, M2O, owned=True
)


class EscalateTicketParams(ActionParams):
    ticket_id: str = target(Ticket)
    agent_id: str
    reason: str | None = None


@_ontology.action(
    EscalateTicketParams,
    target=Ticket,
    roles=["Agent", "Manager"],
    display_name="Escalate Ticket",
    description="Opens an Escalation for a Ticket and assigns an Agent.",
    api_name="EscalateTicket",
)
def _escalate_ticket(ctx: ActionContext, params: EscalateTicketParams) -> dict[str, str]:
    """Open an assigned escalation only when the ticket has no open escalation."""
    ticket = ctx.get(Ticket, params.ticket_id)
    if ticket is None:
        raise ActionError(
            f"ticket {params.ticket_id!r} does not exist",
            code="PRECONDITION_FAILED",
        )
    if any(
        escalation.state == "open"
        for escalation in ctx.traverse(escalationOnTicket, ticket, reverse=True)
    ):
        raise ActionError("already escalated", code="PRECONDITION_FAILED")
    escalation = ctx.create(Escalation, reason=params.reason, state="open")
    ctx.link(escalationOnTicket, escalation, ticket)
    ctx.link(escalationAssignedTo, escalation, params.agent_id)
    return {"escalation_id": escalation.id}


class ResolveEscalationParams(ActionParams):
    escalation_id: str = target(Escalation)


@_ontology.action(
    ResolveEscalationParams,
    target=Escalation,
    roles=["Agent", "Manager"],
    api_name="ResolveEscalation",
    display_name="Resolve Escalation",
    description="Resolves an open Escalation.",
)
def _resolve_escalation(ctx: ActionContext, params: ResolveEscalationParams) -> dict[str, str]:
    """Resolve an open escalation, refusing repeated resolution."""
    escalation = ctx.get(Escalation, params.escalation_id)
    if escalation is None:
        raise ActionError(
            f"escalation {params.escalation_id!r} does not exist",
            code="PRECONDITION_FAILED",
        )
    if escalation.state != "open":
        raise ActionError("escalation is not open", code="TRANSITION_NOT_ALLOWED")
    escalation.state = "resolved"
    ctx.save(escalation)
    return {"escalation_id": params.escalation_id}


class ArchiveEscalationParams(ActionParams):
    escalation_id: str = target(Escalation)


@_ontology.action(
    ArchiveEscalationParams,
    target=Escalation,
    roles=["Manager"],
    api_name="ArchiveEscalation",
    display_name="Archive Escalation",
    description="Retires an Escalation and closes its owned links.",
)
def _archive_escalation(ctx: ActionContext, params: ArchiveEscalationParams) -> dict[str, str]:
    """Retire the escalation; the retirement cascade closes its owned links."""
    ctx.retire(Escalation, params.escalation_id)
    return {"escalation_id": params.escalation_id}


class IsTicketEscalatedParams(FunctionParams):
    ticket_id: str


@_ontology.function(
    IsTicketEscalatedParams,
    description="Whether a Ticket has an open Escalation.",
    input_description="A ticket_id.",
    output_description="True when a visible open Escalation is linked to the Ticket.",
    api_name="isTicketEscalated",
)
def _is_ticket_escalated(query: BoundQuery, params: IsTicketEscalatedParams) -> bool:
    """Answer the per-ticket question through consumer-scoped traversal."""
    return any(
        escalation.payload["state"] == "open"
        for escalation in query.traverse("escalationOnTicket", params.ticket_id, reverse=True)
    )


class TicketStatsParams(FunctionParams):
    queue_id: str


@_ontology.function(
    TicketStatsParams,
    description="Mean ticket age (hours) for a queue.",
    input_description="A queue_id.",
    output_description="A mean age_hours value.",
    api_name="ticketStats",
)
def _ticket_stats(query: BoundQuery, params: TicketStatsParams) -> float | int:
    mean = query.aggregate("Ticket", "age_hours", where={"queue_id": params.queue_id})
    return mean


# Freezes `_ontology` (further `.object()`/`.link()`/`.action()`/
# `.function()` calls would now raise) and validates the assembled
# registry + policy once, at import time.
_ontology.validate()


def build_ontology() -> tuple[Ontology, ObjectStore]:
    """Return the shared validated ontology paired with a fresh object store."""
    return _ontology, ObjectStore(_ontology.registry)


__all__ = [
    "Org",
    "Queue",
    "Agent",
    "Ticket",
    "Comment",
    "Escalation",
    "queueOfOrg",
    "ticketInQueue",
    "commentOnTicket",
    "commentByAgent",
    "escalationOnTicket",
    "escalationAssignedTo",
    "EscalateTicketParams",
    "ResolveEscalationParams",
    "ArchiveEscalationParams",
    "IsTicketEscalatedParams",
    "build_ontology",
]
