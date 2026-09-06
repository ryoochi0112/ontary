"""The living AC4 demo (declared-contracts §3 AC4, task T8 review anchor):
`examples/tickets`' `Ticket.escalated` is declared ontology-owned
(`owned={"escalated": False}`) while `status` stays source-backed.

This test drives the FULL governed path -- connector ingest, not
`store.update` directly, and `EscalateTicket` through the real
`ActionExecutor`/`OntologyClient.execute`, not a raw store write -- to prove
the seam the spec's problem statement calls out is closed end to end:

    ingest (status="open") -> EscalateTicket via executor -> re-ingest the
    SAME ticket with status="closed" from source -> `escalated` still True,
    `status` refreshed to "closed", and the escalation's audit entry carries
    the real `WriteRecord`s + a real `target_id` (not a guess).
"""

from __future__ import annotations

from datetime import datetime, timezone

from examples.tickets.connector import TicketsCSVLikeSource, build_tickets_mapping_spec
from examples.tickets.ontology import (
    NOTIFY_TICKET_ESCALATION,
    TicketEscalationNotification,
    build_ontology,
)
from ontary.client import OntologyClient
from ontary.connect import oid, run_pipeline
from ontary.effects import EffectMeta, EffectPayload
from ontary.security import Consumer
from ontary.store import WriteRecord

_RAW_OPEN = {
    "orgs": [{"org_id": "o1", "name": "Acme Support"}],
    "queues": [{"queue_id": "q1", "org_id": "o1", "name": "Billing"}],
    "agents": [{"agent_id": "a1", "display_name": "Ada", "email": "ada@acme.test"}],
    "tickets": [
        {"ticket_id": "t1", "queue_id": "q1", "subject": "Invoice mismatch",
         "age_hours": 4.0, "status": "open"},
    ],
}

_RAW_CLOSED = {
    "orgs": [{"org_id": "o1", "name": "Acme Support"}],
    "queues": [{"queue_id": "q1", "org_id": "o1", "name": "Billing"}],
    "agents": [{"agent_id": "a1", "display_name": "Ada", "email": "ada@acme.test"}],
    "tickets": [
        {"ticket_id": "t1", "queue_id": "q1", "subject": "Invoice mismatch",
         "age_hours": 4.0, "status": "closed"},
    ],
}


def test_ingest_escalate_reingest_survives_via_governed_path() -> None:
    ontology, store = build_ontology()
    connector = TicketsCSVLikeSource(extracted_at=datetime(2026, 7, 23, tzinfo=timezone.utc))
    mapping = build_tickets_mapping_spec()

    # 1. Connector ingest lands the ticket with status="open"; `escalated`
    #    is not supplied by source (it's ontology-owned) -- the declared
    #    default (False) is injected on insert.
    report = run_pipeline(connector, mapping, ontology, store, raw=_RAW_OPEN, run_at="run-1")
    assert report.ok, report.errors

    ticket_id = oid("tickets_csv", "Ticket", "t1")
    ticket = store.read_current("Ticket", ticket_id)
    assert ticket is not None
    assert ticket.payload["status"] == "open"
    assert ticket.payload["escalated"] is False

    # 2. EscalateTicket through the real governed path: OntologyClient ->
    #    ActionExecutor.execute -- not store.update directly.
    queue_id = oid("tickets_csv", "Queue", "q1")
    consumer = Consumer(
        actor_id="agent-1", role="Agent", scope_level="queue", scope_id=queue_id, kind="human"
    )
    dispatched: list[EffectPayload] = []

    def record_effect(payload: EffectPayload, _meta: EffectMeta) -> None:
        dispatched.append(payload)

    client = OntologyClient(
        ontology,
        store,
        consumer,
        effects={NOTIFY_TICKET_ESCALATION: record_effect},
    )

    result = client.execute("EscalateTicket", {"ticket_id": ticket_id})
    assert result == {"ticket_id": ticket_id}

    escalated_ticket = store.read_current("Ticket", ticket_id)
    assert escalated_ticket is not None
    assert escalated_ticket.payload["escalated"] is True
    assert escalated_ticket.payload["status"] == "open"
    assert dispatched == [
        TicketEscalationNotification(ticket_id=ticket_id, reason=None)
    ]

    # The escalation's audit entry carries the real WriteRecord(s) + a real
    # target_id (declared-contracts AC11) -- not a guessed value.
    entries = store.audit_entries()
    escalate_entry = next(
        entry
        for entry in entries
        if entry.action == "EscalateTicket" and entry.outcome == "ok"
    )
    assert escalate_entry.action == "EscalateTicket"
    assert escalate_entry.outcome == "ok"
    assert escalate_entry.target_id == ticket_id
    assert escalate_entry.writes == [
        WriteRecord(op="update", object_type="Ticket", object_id=ticket_id),
    ]
    finalized_entry = next(
        entry
        for entry in entries
        if entry.action == "EscalateTicket"
        and entry.outcome == "effects_dispatched"
    )
    assert [effect.api_name for effect in finalized_entry.effects] == [
        "NotifyTicketEscalation"
    ]
    assert [effect.outcome for effect in finalized_entry.effects] == [
        "dispatched"
    ]

    # 3. Re-ingest the SAME ticket from source with status changed to
    #    "closed" -- the source-backed property refreshes, and the
    #    ontology-owned `escalated` (written only by the action, never by
    #    source) survives the merge (spec AC4/AC5).
    report2 = run_pipeline(connector, mapping, ontology, store, raw=_RAW_CLOSED, run_at="run-2")
    assert report2.ok, report2.errors

    final_ticket = store.read_current("Ticket", ticket_id)
    assert final_ticket is not None
    assert final_ticket.payload["status"] == "closed"
    assert final_ticket.payload["escalated"] is True
