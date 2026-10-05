"""Lifecycle checks for the tickets example's derived escalation state."""

from __future__ import annotations

from examples.tickets.ontology import (
    Agent,
    ArchiveEscalationParams,
    EscalateTicketParams,
    Escalation,
    IsTicketEscalatedParams,
    Org,
    Queue,
    ResolveEscalationParams,
    Ticket,
    build_ontology,
    escalationAssignedTo,
    escalationOnTicket,
    queueOfOrg,
    ticketInQueue,
)
from ontary.client import OntologyClient
from ontary.testing import consumer, scenario


def test_spec_behavior_check_ticket_escalate_resolve_lifecycle() -> None:
    """An agent escalates, resolves, and re-escalates a ticket through its lifecycle."""
    ontology, store = build_ontology()
    agent = consumer(
        actor_id="agent-1", role="Agent", scope_level="queue", scope_id="billing"
    )
    manager = consumer(
        actor_id="manager-1", role="Manager", scope_level="queue", scope_id="billing"
    )
    onboarding_agent = consumer(
        actor_id="agent-2", role="Agent", scope_level="queue", scope_id="onboarding"
    )

    flow = (
        scenario(ontology, store=store)
        .given(
            Org(id="org-1", name="Example Support"),
            Queue(id="billing", name="Billing"),
            Queue(id="onboarding", name="Onboarding"),
            Agent(id="agent-1", display_name="Ada"),
            Agent(id="agent-2", display_name="Bo"),
            Ticket(
                id="t1",
                subject="Invoice mismatch",
                age_hours=4.0,
                status="open",
                queue_id="billing",
            ),
        )
        .given_link(queueOfOrg, "billing", "org-1")
        .given_link(queueOfOrg, "onboarding", "org-1")
        .given_link(ticketInQueue, "t1", "billing")
    )
    agent_client = OntologyClient(ontology, store, agent)
    onboarding_client = OntologyClient(ontology, store, onboarding_agent)

    flow.when(
        EscalateTicketParams(ticket_id="t1", agent_id="agent-1"), by=agent
    ).then_result({"escalation_id": "id-2"})
    flow.then(Escalation, "id-2", state="open")
    flow.then_link(escalationOnTicket, "id-2", "t1")
    flow.then_link(escalationAssignedTo, "id-2", "agent-1")
    assert agent_client.call_function(
        IsTicketEscalatedParams(ticket_id="t1")
    ) is True

    # The Function obeys the same ticket scope as direct reads.
    assert onboarding_client.call_function(
        IsTicketEscalatedParams(ticket_id="t1")
    ) is False

    flow.when(
        EscalateTicketParams(ticket_id="t1", agent_id="agent-1"), by=agent
    ).then_error("PRECONDITION_FAILED")
    assert len(store.read_all("Escalation")) == 1

    flow.when(ResolveEscalationParams(escalation_id="id-2"), by=agent).then(
        Escalation, "id-2", state="resolved"
    )
    assert agent_client.call_function(
        IsTicketEscalatedParams(ticket_id="t1")
    ) is False

    flow.when(ResolveEscalationParams(escalation_id="id-2"), by=agent).then_error(
        "TRANSITION_NOT_ALLOWED"
    )
    resolved = store.read_current("Escalation", "id-2")
    assert resolved is not None
    assert resolved.payload["state"] == "resolved"

    flow.when(
        EscalateTicketParams(ticket_id="t1", agent_id="agent-1"), by=agent
    ).then_result({"escalation_id": "id-7"})
    flow.then(Escalation, "id-7", state="open")
    assert len(store.read_all("Escalation")) == 2
    assert agent_client.call_function(
        IsTicketEscalatedParams(ticket_id="t1")
    ) is True

    flow.when(ArchiveEscalationParams(escalation_id="id-7"), by=manager).then_result(
        {"escalation_id": "id-7"}
    )
    flow.then_absent(Escalation, "id-7")
    flow.then_no_link(escalationOnTicket, "id-7", "t1")
    flow.then_no_link(escalationAssignedTo, "id-7", "agent-1")
    assert agent_client.call_function(
        IsTicketEscalatedParams(ticket_id="t1")
    ) is False
