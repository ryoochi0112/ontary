"""MCP event discovery and reads: `list_event_types` and `list_events` (#109)."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from mcp.server.mcpserver import MCPServer

from ontary import ActionContext, ActionParams, Ontology, OntologyObject, Source, prop, target
from ontary.audit import AuditEntry, EmittedEvent
from ontary.mcp_server import build_mcp_server
from ontary.meta import Sensitivity
from ontary.model import Event
from ontary.scope import DirectProperty
from ontary.store.inmemory import InMemoryStore
from ontary.testing import consumer

SOURCE = Source(source_system="seed")
INSTANT = datetime(2026, 10, 7, 9, 12, 3, 120000, tzinfo=timezone.utc)


class _Clock:
    """A store clock whose reading a test moves between action calls."""

    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


@dataclass
class EventFixture:
    ontology: Ontology
    store: InMemoryStore
    server: MCPServer
    ticket: type[OntologyObject]
    escalated: type[Event]
    clock: _Clock


def build_fixture(
    *, orphan_event: bool = False, extra_emitters: bool = False, unscoped_ticket: bool = False,
) -> EventFixture:
    """`Ticket` (scoped by `team`), event `TicketEscalated` emitted by
    `escalate_ticket`, consumer `ai` scoped to team `a`.

    `orphan_event` adds `TicketNoted`, which no action emits.
    `extra_emitters` adds `Alert` (also scoped by `team`) and actions emitting
    `TicketEscalated` on it, declared out of alphabetical order.
    `unscoped_ticket` declares `Ticket` unscoped (no row_visibility either).
    The store clock reads `INSTANT` until a test moves `fixture.clock.now`.
    """
    ontology = Ontology("mcp-events", scope_levels=["team"], min_n=1)
    ticket_scope: Any = (
        "unscoped" if unscoped_ticket
        else [DirectProperty(level="team", property_name="team")]
    )

    @ontology.object(layer="L0", scope=ticket_scope)
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
    clock = _Clock(INSTANT)
    store.bind_clock(clock)
    store.insert("Ticket", {"id": "T-1", "team": "a"}, SOURCE)
    store.insert("Ticket", {"id": "T-2", "team": "b"}, SOURCE)
    ai = consumer(actor_id="ai", role="Agent", scope_level="team", scope_id="a", kind="ai")
    server = build_mcp_server(ontology, store, ai)
    return EventFixture(ontology, store, server, Ticket, TicketEscalated, clock)


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


# -- list_events (T4) ----------------------------------------------------------

ROW_KEYS = {
    "event_type", "about_type", "about_id", "ts", "invocation_id", "payload", "redacted_fields",
}


def _escalate(fixture: EventFixture, ticket_id: str, *, team: str, at: datetime | None = None) -> None:
    """Run `escalate_ticket` as an operator of `team`, at `at` when given."""
    if at is not None:
        fixture.clock.now = at
    operator = consumer(actor_id="op", role="Agent", scope_level="team", scope_id=team)
    client = fixture.ontology.bind(fixture.store, clock=fixture.clock).for_consumer(operator)
    client.execute("escalate_ticket", {"ticket_id": ticket_id})


def _list(fixture: EventFixture, **arguments: Any) -> dict[str, Any]:
    return _call(fixture.server, "list_events", arguments)


def _error_code(payload: dict[str, Any]) -> str:
    assert "result" not in payload, payload
    assert set(payload) == {"error"}, payload
    code: str = payload["error"]["code"]
    return code


def _ids(payload: dict[str, Any]) -> list[str]:
    return [row["about_id"] for row in payload["result"]]


def test_list_events_row_shape_and_values() -> None:
    fixture = build_fixture()
    _escalate(fixture, "T-1", team="a")
    _escalate(fixture, "T-2", team="b")
    payload = _list(
        fixture, event_type="TicketEscalated", about_type="Ticket", about_id="T-1", limit=10,
    )
    entry = fixture.store.audit_entries()[0]
    assert payload == {
        "result": [{
            "event_type": "TicketEscalated",
            "about_type": "Ticket",
            "about_id": "T-1",
            "ts": INSTANT.isoformat(),
            "invocation_id": entry.invocation_id,
            "payload": {"reason": "sla"},
            "redacted_fields": ["customer_email"],
        }],
        "next_cursor": None,
        "has_more": False,
        "scope_limited": True,
    }
    row = payload["result"][0]
    assert set(row) == ROW_KEYS
    ts = datetime.fromisoformat(row["ts"])
    assert ts.tzinfo is not None and ts.utcoffset() is not None
    assert ts == entry.ts


def test_hidden_subject_events_are_not_listed() -> None:
    fixture = build_fixture()
    _escalate(fixture, "T-2", team="b")
    _escalate(fixture, "T-1", team="a")
    payload = _list(fixture, event_type="TicketEscalated")
    assert _ids(payload) == ["T-1"]
    assert all(row["about_id"] != "T-2" for row in payload["result"])
    assert _list(fixture, about_type="Ticket", about_id="T-2")["result"] == []


def test_retired_subject_events_stay_listed() -> None:
    fixture = build_fixture()
    _escalate(fixture, "T-1", team="a")
    fixture.store.retire_object("Ticket", "T-1")
    assert _ids(_list(fixture, event_type="TicketEscalated")) == ["T-1"]


def test_subject_moved_out_of_scope_then_retired_hides_its_events() -> None:
    fixture = build_fixture()
    _escalate(fixture, "T-1", team="a")
    fixture.store.update("Ticket", "T-1", {"team": "b"}, SOURCE)
    fixture.store.retire_object("Ticket", "T-1")
    assert _list(fixture, event_type="TicketEscalated")["result"] == []
    assert _list(fixture, about_type="Ticket", about_id="T-1")["result"] == []


def test_redacted_payload_key_is_absent_not_null() -> None:
    fixture = build_fixture()
    _escalate(fixture, "T-1", team="a")
    row = _list(fixture)["result"][0]
    assert "customer_email" not in row["payload"]
    assert row["redacted_fields"] == ["customer_email"]
    assert row["payload"]["reason"] == "sla"


def test_human_consumer_sees_the_ai_hidden_field() -> None:
    fixture = build_fixture()
    _escalate(fixture, "T-1", team="a")
    human = consumer(actor_id="h", role="Agent", scope_level="team", scope_id="a")
    server = build_mcp_server(fixture.ontology, fixture.store, human)
    row = _call(server, "list_events", {})["result"][0]
    assert row["payload"] == {"reason": "sla", "customer_email": "x@example.com"}
    assert row["redacted_fields"] == []


def _five_visible(fixture: EventFixture) -> None:
    """Five visible T-1 events, with hidden T-2 events interleaved."""
    for index in range(5):
        _escalate(fixture, "T-1", team="a", at=INSTANT + timedelta(seconds=index))
        _escalate(fixture, "T-2", team="b", at=INSTANT + timedelta(seconds=index))


def test_paging_two_two_one_concatenates_to_the_unpaged_order() -> None:
    fixture = build_fixture()
    _five_visible(fixture)
    unpaged = _list(fixture)
    assert len(unpaged["result"]) == 5
    assert unpaged["has_more"] is False and unpaged["next_cursor"] is None

    pages = [_list(fixture, limit=2)]
    while pages[-1]["has_more"]:
        pages.append(_list(fixture, limit=2, after=pages[-1]["next_cursor"]))
    assert [len(page["result"]) for page in pages] == [2, 2, 1]
    assert [page["has_more"] for page in pages] == [True, True, False]
    assert [page["next_cursor"] is None for page in pages] == [False, False, True]
    assert [row for page in pages for row in page["result"]] == unpaged["result"]
    assert [row["ts"] for row in unpaged["result"]] == [
        (INSTANT + timedelta(seconds=index)).isoformat() for index in range(5)
    ]
    assert all(set(page) == {"result", "next_cursor", "has_more", "scope_limited"} for page in pages)


def test_cursor_is_the_log_position_of_the_last_row() -> None:
    fixture = build_fixture()
    _five_visible(fixture)
    # Audit entries alternate T-1 (even index) and hidden T-2 (odd index).
    first = _list(fixture, limit=2)
    assert first["next_cursor"] == "ev1.2.0"
    second = _list(fixture, limit=2, after=first["next_cursor"])
    assert second["next_cursor"] == "ev1.6.0"
    entries = fixture.store.audit_entries()
    assert [row["invocation_id"] for row in second["result"]] == [
        entries[4].invocation_id, entries[6].invocation_id,
    ]


def test_cursor_splits_events_inside_one_audit_entry() -> None:
    fixture = build_fixture()
    fixture.store.append_audit(AuditEntry(
        ts=INSTANT, kind="action", invocation_id="inv-multi", actor="op", role="Agent",
        action="escalate_ticket", target_type="Ticket", outcome="ok",
        events=[
            EmittedEvent(
                event_type="TicketEscalated", about_type="Ticket", about_id="T-1",
                payload={"reason": reason, "customer_email": None},
            )
            for reason in ("first", "second", "third")
        ],
    ))
    reasons: list[str] = []
    cursors: list[str | None] = []
    page = _list(fixture, limit=1)
    while True:
        reasons += [row["payload"]["reason"] for row in page["result"]]
        cursors.append(page["next_cursor"])
        if not page["has_more"]:
            break
        page = _list(fixture, limit=1, after=page["next_cursor"])
    assert reasons == ["first", "second", "third"]
    assert cursors == ["ev1.0.0", "ev1.0.1", None]


def test_include_total_counts_every_visible_row_on_every_page() -> None:
    fixture = build_fixture()
    _five_visible(fixture)
    first = _list(fixture, limit=2, include_total=True)
    assert first["total"] == 5
    second = _list(fixture, limit=2, after=first["next_cursor"], include_total=True)
    assert second["total"] == 5
    assert "total" not in _list(fixture, limit=2)
    assert _list(fixture, event_type="TicketEscalated", about_type="Ticket", about_id="T-2",
                 include_total=True)["total"] == 0


def test_count_only_mode() -> None:
    fixture = build_fixture()
    _five_visible(fixture)
    assert _list(fixture, limit=0, include_total=True) == {
        "result": [], "next_cursor": None, "has_more": True, "total": 5, "scope_limited": True,
    }
    empty = build_fixture()
    assert _list(empty, limit=0, include_total=True) == {
        "result": [], "next_cursor": None, "has_more": False, "total": 0, "scope_limited": True,
    }


@pytest.mark.parametrize(
    ("arguments", "code"),
    [
        ({"limit": 1001}, "INVALID_LIMIT"),
        ({"limit": 0}, "INVALID_LIMIT"),
        ({"limit": -1}, "INVALID_LIMIT"),
        ({"limit": 0, "include_total": True, "after": "ev1.0.0"}, "INVALID_LIMIT"),
        ({"after": "ev1.0.0"}, "AFTER_WITHOUT_LIMIT"),
        ({"limit": 1001, "after": "nope"}, "INVALID_LIMIT"),
        ({"limit": "2"}, "INVALID_PARAMS"),
        ({"include_total": "yes"}, "INVALID_PARAMS"),
        ({"event_type": 5}, "INVALID_PARAMS"),
    ],
)
def test_paging_refusals(arguments: dict[str, Any], code: str) -> None:
    fixture = build_fixture()
    _five_visible(fixture)
    assert _error_code(_list(fixture, **arguments)) == code


def test_limit_of_exactly_max_is_accepted() -> None:
    fixture = build_fixture()
    _escalate(fixture, "T-1", team="a")
    assert _ids(_list(fixture, limit=1000)) == ["T-1"]


def test_time_window_since_inclusive_until_exclusive() -> None:
    fixture = build_fixture()
    t1, t2, t3 = (INSTANT + timedelta(hours=hours) for hours in (1, 2, 3))
    for at in (t1, t2, t3):
        _escalate(fixture, "T-1", team="a", at=at)

    def times(**window: Any) -> list[str]:
        return [row["ts"] for row in _list(fixture, **window)["result"]]

    assert times(since=t2.isoformat(), until=t3.isoformat()) == [t2.isoformat()]
    assert times(since=t2.isoformat()) == [t2.isoformat(), t3.isoformat()]
    assert times(until=t2.isoformat()) == [t1.isoformat()]
    tokyo = timezone(timedelta(hours=9))
    assert times(since=t2.astimezone(tokyo).isoformat()) == [t2.isoformat(), t3.isoformat()]
    assert times(since=t2.isoformat().replace("+00:00", "Z")) == [
        t2.isoformat(), t3.isoformat(),
    ]


@pytest.mark.parametrize(
    "window",
    [
        {"since": "2026-10-07T00:00:00"},
        {"until": "2026-10-07T00:00:00"},
        {"since": "2026-10-07"},
        {"since": "yesterday"},
        {"until": "not-a-time"},
        {"since": 5},
    ],
)
def test_naive_or_unparseable_window_refuses_invalid_params(window: dict[str, Any]) -> None:
    fixture = build_fixture()
    assert _error_code(_list(fixture, **window)) == "INVALID_PARAMS"


def test_unknown_names_refuse_with_exact_codes() -> None:
    fixture = build_fixture()
    assert _error_code(_list(fixture, event_type="Nope")) == "UNKNOWN_EVENT_TYPE"
    assert _error_code(_list(fixture, about_type="Nope", about_id="T-1")) == "UNKNOWN_OBJECT_TYPE"
    assert _error_code(_list(fixture, about_type="Nope")) == "UNKNOWN_OBJECT_TYPE"
    assert _error_code(_list(fixture, about_id="T-1")) == "INVALID_PARAMS"


def test_validation_order() -> None:
    fixture = build_fixture()
    assert _error_code(_list(fixture, event_type="Nope", about_type="Nope")) == "UNKNOWN_EVENT_TYPE"
    assert _error_code(_list(fixture, about_type="Nope", limit=5000)) == "UNKNOWN_OBJECT_TYPE"
    assert _error_code(_list(fixture, event_type="Nope", after="ev1.0.0")) == "UNKNOWN_EVENT_TYPE"
    assert _error_code(_list(fixture, about_type="Nope", since="naive")) == "UNKNOWN_OBJECT_TYPE"
    assert _error_code(_list(fixture, since="naive", limit=5000)) == "INVALID_PARAMS"
    assert _error_code(_list(fixture, since="naive", after="ev1.0.0")) == "INVALID_PARAMS"
    assert _error_code(_list(fixture, after="nope")) == "AFTER_WITHOUT_LIMIT"


def test_about_type_without_about_id_filters_by_subject_type() -> None:
    fixture = build_fixture(extra_emitters=True)
    _escalate(fixture, "T-1", team="a")
    fixture.store.insert("Alert", {"id": "A-1", "team": "a"}, SOURCE)
    fixture.store.append_audit(AuditEntry(
        ts=INSTANT, kind="action", invocation_id="inv-alert", actor="op", role="Agent",
        action="page_alert", target_type="Alert", outcome="ok",
        events=[EmittedEvent(
            event_type="TicketEscalated", about_type="Alert", about_id="A-1",
            payload={"reason": "page", "customer_email": None},
        )],
    ))
    assert [row["about_type"] for row in _list(fixture)["result"]] == ["Ticket", "Alert"]
    assert [row["about_id"] for row in _list(fixture, about_type="Alert")["result"]] == ["A-1"]
    assert [row["about_id"] for row in _list(fixture, about_type="Ticket")["result"]] == ["T-1"]


def test_about_type_alone_still_applies_scope_and_redaction() -> None:
    fixture = build_fixture()
    _escalate(fixture, "T-1", team="a")
    _escalate(fixture, "T-2", team="b")
    payload = _list(fixture, about_type="Ticket", include_total=True)
    assert _ids(payload) == ["T-1"]
    assert payload["total"] == 1
    row = payload["result"][0]
    assert "customer_email" not in row["payload"]
    assert row["redacted_fields"] == ["customer_email"]


def test_redacted_fields_are_sorted_not_declaration_order() -> None:
    ontology = Ontology("mcp-events-sorted", scope_levels=["team"], min_n=1)

    @ontology.object(layer="L0", scope=[DirectProperty(level="team", property_name="team")])
    class Ticket(OntologyObject):
        id: str = prop(primary_key=True)
        team: str

    @ontology.event()
    class TicketFlagged(Event):
        reason: str
        zeta_note: str | None = prop(default=None, sensitivity=Sensitivity(ai_usable=False))
        alpha_email: str | None = prop(default=None, sensitivity=Sensitivity(ai_usable=False))

    class FlagTicket(ActionParams):
        ticket_id: str = target(Ticket)

    @ontology.action(
        FlagTicket, target=Ticket, roles=["Agent"], api_name="flag_ticket",
        emits=[TicketFlagged],
    )
    def flag_ticket(ctx: ActionContext, params: FlagTicket) -> dict[str, Any]:
        ctx.emit(TicketFlagged(reason="sla", zeta_note="z", alpha_email="a@example.com"))
        return {}

    store = InMemoryStore(ontology.registry)
    store.insert("Ticket", {"id": "T-1", "team": "a"}, SOURCE)
    operator = consumer(actor_id="op", role="Agent", scope_level="team", scope_id="a")
    ontology.bind(store).for_consumer(operator).execute("flag_ticket", {"ticket_id": "T-1"})
    ai = consumer(actor_id="ai", role="Agent", scope_level="team", scope_id="a", kind="ai")
    row = _call(build_mcp_server(ontology, store, ai), "list_events", {})["result"][0]
    assert row["payload"] == {"reason": "sla"}
    assert row["redacted_fields"] == ["alpha_email", "zeta_note"]


def test_scope_limited_is_true_for_a_scoped_subject_with_no_events() -> None:
    fixture = build_fixture()
    assert fixture.store.audit_entries() == []
    payload = _list(fixture)
    assert payload["result"] == []
    assert payload["scope_limited"] is True
    assert _list(fixture, event_type="TicketEscalated")["scope_limited"] is True


def test_scope_limited_is_false_for_an_unscoped_subject() -> None:
    fixture = build_fixture(unscoped_ticket=True)
    _escalate(fixture, "T-1", team="a")
    _escalate(fixture, "T-2", team="b")
    payload = _list(fixture, event_type="TicketEscalated")
    assert payload["scope_limited"] is False
    assert _ids(payload) == ["T-1", "T-2"]


def test_scope_limited_is_false_for_an_event_no_action_emits() -> None:
    fixture = build_fixture(orphan_event=True)
    payload = _list(fixture, event_type="TicketNoted")
    assert payload == {"result": [], "next_cursor": None, "has_more": False, "scope_limited": False}
    assert _list(fixture, event_type="TicketNoted", about_type="Ticket")["scope_limited"] is True


def test_cursor_past_the_log_end_is_an_empty_last_page() -> None:
    fixture = build_fixture()
    _escalate(fixture, "T-1", team="a")
    assert _list(fixture, limit=2, after="ev1.999.0") == {
        "result": [], "next_cursor": None, "has_more": False, "scope_limited": True,
    }
    assert _list(fixture, limit=2, after="ev1.0.0")["result"] == []
    assert _ids(_list(fixture, limit=2, after="ev1.0.0", include_total=True)) == []


@pytest.mark.parametrize(
    "after", ["nope", "ev1.x.0", "ev2.0.0", "ev1.0", "ev1.0.0.0", "ev1.-1.0", " ev1.0.0", ""],
)
def test_malformed_cursor_refuses_invalid_cursor(after: str) -> None:
    fixture = build_fixture()
    _escalate(fixture, "T-1", team="a")
    assert _error_code(_list(fixture, limit=2, after=after)) == "INVALID_CURSOR"


def test_list_events_is_read_only_annotated() -> None:
    fixture = build_fixture()
    tools = {tool.name: tool for tool in asyncio.run(fixture.server.list_tools())}
    annotations = tools["list_events"].annotations
    assert annotations is not None
    assert annotations.read_only_hint is True
    assert set(tools["list_events"].input_schema["properties"]) == {
        "event_type", "about_type", "about_id", "since", "until", "limit", "after",
        "include_total",
    }
