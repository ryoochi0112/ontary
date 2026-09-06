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

from ontary import Source, Store

_SRC = Source(source_system="synthetic")


def load_fixtures(store: Store) -> dict[str, str]:
    """Seed one Org, two Queues/Agents, sixteen Tickets, and one Comment."""
    org_id = store.insert("Org", {"name": "Acme Support"}, _SRC)
    q_a = store.insert("Queue", {"name": "Billing"}, _SRC)
    q_b = store.insert("Queue", {"name": "Onboarding"}, _SRC)
    store.create_link("queueOfOrg", q_a, org_id)
    store.create_link("queueOfOrg", q_b, org_id)

    agent_1 = store.insert("Agent", {"display_name": "Ada", "email": "ada@acme.test"}, _SRC)
    agent_2 = store.insert("Agent", {"display_name": "Bo", "email": "bo@acme.test"}, _SRC)

    t1 = store.insert(
        "Ticket",
        {
            "subject": "Invoice mismatch",
            "age_hours": 4.0,
            "status": "open",
            "queue_id": q_a,
            "channel": "email",
        },
        _SRC,
    )
    t2 = store.insert(
        "Ticket",
        {
            "subject": "Refund request",
            "age_hours": 12.0,
            "status": "open",
            "queue_id": q_a,
            "channel": "chat",
        },
        _SRC,
    )
    t3 = store.insert(
        "Ticket",
        {
            "subject": "Welcome call",
            "age_hours": 1.0,
            "status": "open",
            "queue_id": q_b,
            "channel": "phone",
        },
        _SRC,
    )
    ticket_rows = [
        (t1, q_a),
        (t2, q_a),
        (t3, q_b),
    ]
    extra_tickets = [
        ("Account locked", 2.0, "closed", q_a, "chat"),
        ("Payment failed", 18.0, "pending", q_a, "phone"),
        ("Duplicate invoice", 7.0, "closed", q_a, "email"),
        ("Upgrade question", 24.0, "open", q_a, "chat"),
        ("Cancel subscription", 36.0, "pending", q_a, "email"),
        ("Tax receipt needed", 9.0, "open", q_a, "phone"),
        ("Card declined", 15.0, "closed", q_a, "chat"),
        ("Trial extension", 3.0, "open", q_b, "email"),
        ("Setup assistance", 5.0, "closed", q_b, "chat"),
        ("Mobile login issue", 8.0, "pending", q_b, "phone"),
        ("Feature request", 20.0, "open", q_b, "email"),
        ("Data export help", 30.0, "closed", q_b, "chat"),
        ("Phone callback", 48.0, "pending", q_b, "phone"),
    ]
    for subject, age_hours, status, queue_id, channel in extra_tickets:
        ticket_id = store.insert(
            "Ticket",
            {
                "subject": subject,
                "age_hours": age_hours,
                "status": status,
                "queue_id": queue_id,
                "channel": channel,
            },
            _SRC,
        )
        ticket_rows.append((ticket_id, queue_id))

    for ticket_id, queue_id in ticket_rows:
        store.create_link("ticketInQueue", ticket_id, queue_id)

    comment_id = store.insert("Comment", {"text": "Looking into this now."}, _SRC)
    store.create_link("commentOnTicket", comment_id, t1)
    store.create_link("commentByAgent", comment_id, agent_1)

    ids = {
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
    ids.update(
        {
            f"ticket_{number}_id": ticket_id
            for number, (ticket_id, _queue_id) in enumerate(ticket_rows[3:], start=4)
        }
    )
    return ids
