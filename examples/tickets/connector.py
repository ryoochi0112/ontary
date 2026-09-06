"""A tiny second-domain connector for `examples/tickets` (spec
connector-framework.md §3 AC11, ontary task T9): proves the framework holds
zero DSO assumptions -- imports only `ontary.connect` + `examples.tickets.
ontology`, nothing from any other example.

A csv-like source (plain `extract()`, no dlt) landing `orgs`/`queues`/
`agents`/`tickets` rows into the tiny canonical schema below, mapped onto the
EXISTING tickets ontology (`examples.tickets.ontology`).
"""

from __future__ import annotations

from ontary import (
    BaseConnector,
    CanonicalBatch,
    CanonicalRecord,
    LinkBinding,
    MappingSpec,
    ObjectBinding,
    RawTables,
)


class CanonicalOrg(CanonicalRecord):
    org_key: str
    name: str


class CanonicalQueue(CanonicalRecord):
    queue_key: str
    org_key: str
    name: str


class CanonicalAgent(CanonicalRecord):
    agent_key: str
    display_name: str
    email: str | None = None


class CanonicalTicket(CanonicalRecord):
    ticket_key: str
    queue_key: str
    subject: str
    age_hours: float
    status: str = "open"


class TicketsCSVLikeSource(BaseConnector):
    """`SourceConnector` (structural) for a csv-like ticket export: plain
    `extract()` (no dlt), pure `transform()` over `orgs`/`queues`/`agents`/
    `tickets` `RawTables`."""

    name: str = "tickets_csv"

    def extract(self) -> RawTables:
        raise NotImplementedError("wire a real csv/API export here; not needed for the smoke test")

    def transform(self, raw: RawTables) -> CanonicalBatch:
        return CanonicalBatch(
            entities={
                "orgs": [
                    CanonicalOrg(
                        org_key=str(r["org_id"]), name=r["name"], lineage=self.lineage(str(r["org_id"]))
                    )
                    for r in raw.get("orgs", [])
                ],
                "queues": [
                    CanonicalQueue(
                        queue_key=str(r["queue_id"]),
                        org_key=str(r["org_id"]),
                        name=r["name"],
                        lineage=self.lineage(str(r["queue_id"])),
                    )
                    for r in raw.get("queues", [])
                ],
                "agents": [
                    CanonicalAgent(
                        agent_key=str(r["agent_id"]),
                        display_name=r["display_name"],
                        email=r.get("email"),
                        lineage=self.lineage(str(r["agent_id"])),
                    )
                    for r in raw.get("agents", [])
                ],
                "tickets": [
                    CanonicalTicket(
                        ticket_key=str(r["ticket_id"]),
                        queue_key=str(r["queue_id"]),
                        subject=r["subject"],
                        age_hours=float(r["age_hours"]),
                        status=r.get("status", "open"),
                        lineage=self.lineage(str(r["ticket_id"])),
                    )
                    for r in raw.get("tickets", [])
                ],
            }
        )


def build_tickets_mapping_spec() -> MappingSpec:
    """The tickets canonical -> ontology `MappingSpec` (spec AC11)."""
    return MappingSpec(
        object_bindings=[
            ObjectBinding(
                entity="orgs", object_type="Org", key_field="org_key",
                property_map={"name": "name"}, record_model=CanonicalOrg,
            ),
            ObjectBinding(
                entity="queues", object_type="Queue", key_field="queue_key",
                property_map={"name": "name"}, record_model=CanonicalQueue,
            ),
            ObjectBinding(
                entity="agents", object_type="Agent", key_field="agent_key",
                property_map={"display_name": "display_name", "email": "email"},
                record_model=CanonicalAgent,
            ),
            ObjectBinding(
                entity="tickets", object_type="Ticket", key_field="ticket_key",
                property_map={"subject": "subject", "age_hours": "age_hours", "status": "status"},
                record_model=CanonicalTicket,
            ),
        ],
        link_bindings=[
            LinkBinding(
                link_type="queueOfOrg", from_entity="queues", from_key_field="queue_key",
                to_entity="orgs", to_key_field="org_key",
            ),
            LinkBinding(
                link_type="ticketInQueue", from_entity="tickets", from_key_field="ticket_key",
                to_entity="queues", to_key_field="queue_key",
            ),
        ],
    )


__all__ = [
    "CanonicalOrg",
    "CanonicalQueue",
    "CanonicalAgent",
    "CanonicalTicket",
    "TicketsCSVLikeSource",
    "build_tickets_mapping_spec",
]
