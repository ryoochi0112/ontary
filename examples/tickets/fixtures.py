"""The tickets example's synthetic-fixture loader.

`EscalateTicket`/`ticketStats` are declared (handlers included) directly on
`examples.tickets.ontology`'s module-level `Ontology` (spec
`typed-actions.md` §6/AC10) -- there is no separate registration step for
tests/README code to call anymore:

    ontology, store = build_ontology()
    client = OntologyClient(ontology, store, consumer)
    ids = load_fixtures(store)
"""

from __future__ import annotations

from ontary import ObjectStore, Source

_SRC = Source(source_system="synthetic")


def load_fixtures(store: ObjectStore) -> dict[str, str]:
    """Seed one Org / two Queues / two Agents / three Tickets / one Comment."""
    org_id = store.insert("Org", {"name": "Acme Support"}, _SRC)
    q_a = store.insert("Queue", {"name": "Billing"}, _SRC)
    q_b = store.insert("Queue", {"name": "Onboarding"}, _SRC)
    store.create_link("queueOfOrg", q_a, org_id)
    store.create_link("queueOfOrg", q_b, org_id)

    agent_1 = store.insert("Agent", {"display_name": "Ada", "email": "ada@acme.test"}, _SRC)
    agent_2 = store.insert("Agent", {"display_name": "Bo", "email": "bo@acme.test"}, _SRC)

    t1 = store.insert(
        "Ticket",
        {"subject": "Invoice mismatch", "age_hours": 4.0, "status": "open", "queue_id": q_a},
        _SRC,
    )
    t2 = store.insert(
        "Ticket",
        {"subject": "Refund request", "age_hours": 12.0, "status": "open", "queue_id": q_a},
        _SRC,
    )
    t3 = store.insert(
        "Ticket",
        {"subject": "Welcome call", "age_hours": 1.0, "status": "open", "queue_id": q_b},
        _SRC,
    )
    store.create_link("ticketInQueue", t1, q_a)
    store.create_link("ticketInQueue", t2, q_a)
    store.create_link("ticketInQueue", t3, q_b)

    comment_id = store.insert("Comment", {"text": "Looking into this now."}, _SRC)
    store.create_link("commentOnTicket", comment_id, t1)
    store.create_link("commentByAgent", comment_id, agent_1)

    return {
        "org_id": org_id,
        "queue_a_id": q_a,
        "queue_b_id": q_b,
        "agent_1_id": agent_1,
        "agent_2_id": agent_2,
        "ticket_1_id": t1,
        "ticket_2_id": t2,
        "ticket_3_id": t3,
        "comment_id": comment_id,
    }
