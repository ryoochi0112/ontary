"""Runnable connector example (spec AC15): builds the tickets connector,
runs `run_pipeline` against an in-memory store on inline raw rows -- no
files, no network, no sqlite -- and prints the resulting `RunReport`.

Run it with:

    uv run python -m examples.tickets.run_connector

This is the "connector authoring" half of the SDK's front door made
concrete and runnable, complementing the README quickstart (which shows
the "author + read" half) and `examples/tickets/connector.py` (the
importable connector + `MappingSpec` this script drives).
"""

from __future__ import annotations

from datetime import datetime, timezone

from examples.tickets.connector import TicketsCSVLikeSource, build_tickets_mapping_spec
from examples.tickets.ontology import build_ontology
from ontary import InMemoryStore, RawTables, oid, run_pipeline

# Inline "raw" rows standing in for a real CSV/API export -- exactly the
# `RawTables` shape `TicketsCSVLikeSource.transform()` expects. `run_pipeline`
# is given `raw=` directly so `connector.extract()` (which raises
# `NotImplementedError` -- there's no real source wired here) is never
# called.
RAW_ROWS: RawTables = {
    "orgs": [{"org_id": "o1", "name": "Acme Support"}],
    "queues": [{"queue_id": "q1", "org_id": "o1", "name": "Billing"}],
    "agents": [{"agent_id": "a1", "display_name": "Ada", "email": "ada@acme.test"}],
    "tickets": [
        {"ticket_id": "t1", "queue_id": "q1", "subject": "Invoice mismatch", "age_hours": 4.0},
        {"ticket_id": "t2", "queue_id": "q1", "subject": "Refund request", "age_hours": 12.0},
    ],
}


def main() -> None:
    ontology, _ = build_ontology()  # discard the default sqlite ObjectStore
    store = InMemoryStore(ontology.registry)  # dogfood the in-memory Store double

    connector = TicketsCSVLikeSource(extracted_at=datetime(2026, 7, 24, tzinfo=timezone.utc))
    mapping = build_tickets_mapping_spec()

    report = run_pipeline(connector, mapping, ontology, store, raw=RAW_ROWS, run_at="run-1")

    print(f"ok={report.ok}")
    print(f"written={report.written}")
    print(f"links_created={report.links_created}")
    print(f"links_skipped={report.links_skipped}")
    if report.errors:
        print(f"errors={report.errors}")

    # Prove the store actually holds what the report claims, then show a
    # second run against the SAME store is idempotent (spec AC4/AC9/AC11).
    org = store.read_current("Org", oid(connector.name, "Org", "o1"))
    assert org is not None
    print(f"Org o1 payload: {org.payload}")

    report2 = run_pipeline(connector, mapping, ontology, store, raw=RAW_ROWS, run_at="run-2")
    assert report2.ok
    assert report2.written == report.written
    assert report2.links_created == {"queueOfOrg": 0, "ticketInQueue": 0}
    print(f"re-run ok={report2.ok} written={report2.written} links_created={report2.links_created}")


if __name__ == "__main__":
    main()
