"""MCP event discovery: `list_event_types` payload and annotation (#109)."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Any

from mcp.server.mcpserver import MCPServer

from ontary import ActionContext, ActionParams, Ontology, OntologyObject, Source, prop, target
from ontary.mcp_server import build_mcp_server
from ontary.meta import Sensitivity
from ontary.model import Event
from ontary.scope import DirectProperty
from ontary.store.inmemory import InMemoryStore
from ontary.testing import consumer

SOURCE = Source(source_system="seed")


@dataclass
class EventFixture:
    ontology: Ontology
    store: InMemoryStore
    server: MCPServer
    ticket: type[OntologyObject]
    escalated: type[Event]


def build_fixture(*, orphan_event: bool = False, extra_emitters: bool = False) -> EventFixture:
    """`Ticket` (scoped by `team`), event `TicketEscalated` emitted by
    `escalate_ticket`, consumer `ai` scoped to team `a`.

    `orphan_event` adds `TicketNoted`, which no action emits.
    `extra_emitters` adds `Alert` (also scoped by `team`) and actions emitting
    `TicketEscalated` on it, declared out of alphabetical order.
    """
    ontology = Ontology("mcp-events", scope_levels=["team"], min_n=1)

    @ontology.object(layer="L0", scope=[DirectProperty(level="team", property_name="team")])
    class Ticket(OntologyObject):
        id: str = prop(primary_key=True)
        team: str

    @ontology.event()
    class TicketEscalated(Event):
        reason: str
        customer_email: str | None = prop(default=None, sensitivity=Sensitivity(ai_usable=False))

    class EscalateTicket(ActionParams):
        ticket_id: str = target(Ticket)

    @ontology.action(
        EscalateTicket, target=Ticket, roles=["Agent"], api_name="escalate_ticket",
        emits=[TicketEscalated],
    )
    def escalate_ticket(ctx: ActionContext, params: EscalateTicket) -> dict[str, Any]:
        ctx.emit(TicketEscalated(reason="sla", customer_email="x@example.com"))
        return {}

    if orphan_event:
        @ontology.event()
        class TicketNoted(Event):
            note: str

    if extra_emitters:
        @ontology.object(layer="L0", scope=[DirectProperty(level="team", property_name="team")])
        class Alert(OntologyObject):
            id: str = prop(primary_key=True)
            team: str

        class PageAlert(ActionParams):
            alert_id: str = target(Alert)

        class AckAlert(ActionParams):
            alert_id: str = target(Alert)

        @ontology.action(
            PageAlert, target=Alert, roles=["Agent"], api_name="page_alert",
            emits=[TicketEscalated],
        )
        def page_alert(ctx: ActionContext, params: PageAlert) -> dict[str, Any]:
            return {}

        @ontology.action(
            AckAlert, target=Alert, roles=["Agent"], api_name="ack_alert",
            emits=[TicketEscalated],
        )
        def ack_alert(ctx: ActionContext, params: AckAlert) -> dict[str, Any]:
            return {}

    store = InMemoryStore(ontology.registry)
    store.insert("Ticket", {"id": "T-1", "team": "a"}, SOURCE)
    store.insert("Ticket", {"id": "T-2", "team": "b"}, SOURCE)
    ai = consumer(actor_id="ai", role="Agent", scope_level="team", scope_id="a", kind="ai")
    server = build_mcp_server(ontology, store, ai)
    return EventFixture(ontology, store, server, Ticket, TicketEscalated)


def _call(server: MCPServer, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    result = asyncio.run(server.call_tool(name, arguments))
    if result.structured_content is not None:
        return result.structured_content
    payload: dict[str, Any] = json.loads(result.content[0].text)
    return payload


def _property(name: str, type_: str, *, required: bool, ai_usable: bool) -> dict[str, Any]:
    return {
        "name": name,
        "type": type_,
        "choices": None,
        "transitions": None,
        "fields": None,
        "required": required,
        "ai_usable": ai_usable,
        "human_visible": True,
        "scope_level": None,
    }


def test_list_event_types_exact_payload() -> None:
    fixture = build_fixture()
    payload = _call(fixture.server, "list_event_types", {})
    assert set(payload) == {"event_types"}
    assert len(payload["event_types"]) == 1
    entry = payload["event_types"][0]
    assert set(entry) == {"api_name", "description", "properties", "about_types", "emitted_by"}
    assert entry["api_name"] == "TicketEscalated"
    assert entry["about_types"] == ["Ticket"]
    assert entry["emitted_by"] == ["escalate_ticket"]
    assert entry["properties"] == [
        _property("reason", "str", required=True, ai_usable=True),
        _property("customer_email", "str", required=False, ai_usable=False),
    ]


def test_event_properties_use_the_object_type_property_keys() -> None:
    fixture = build_fixture()
    event = _call(fixture.server, "list_event_types", {})["event_types"][0]
    object_type = _call(fixture.server, "list_object_types", {})["object_types"][0]
    for event_property in event["properties"]:
        assert set(event_property) == set(object_type["properties"][0])
    by_name = {p["name"]: p for p in event["properties"]}
    assert by_name["customer_email"]["ai_usable"] is False
    assert by_name["customer_email"]["human_visible"] is True
    assert by_name["reason"]["ai_usable"] is True


def test_event_emitted_by_no_action_has_empty_subject_lists() -> None:
    fixture = build_fixture(orphan_event=True)
    entries = _call(fixture.server, "list_event_types", {})["event_types"]
    assert [e["api_name"] for e in entries] == ["TicketEscalated", "TicketNoted"]
    orphan = entries[1]
    assert orphan["about_types"] == []
    assert orphan["emitted_by"] == []
    assert orphan["properties"] == [_property("note", "str", required=True, ai_usable=True)]


def test_about_types_are_sorted_and_deduplicated_and_emitted_by_sorted() -> None:
    fixture = build_fixture(extra_emitters=True)
    entry = _call(fixture.server, "list_event_types", {})["event_types"][0]
    assert entry["about_types"] == ["Alert", "Ticket"]
    assert entry["emitted_by"] == ["ack_alert", "escalate_ticket", "page_alert"]


def test_list_event_types_is_read_only_annotated() -> None:
    fixture = build_fixture()
    tools = {tool.name: tool for tool in asyncio.run(fixture.server.list_tools())}
    annotations = tools["list_event_types"].annotations
    assert annotations is not None
    assert annotations.read_only_hint is True
