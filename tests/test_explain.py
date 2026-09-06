"""Operator-only explain-why traces for governed reads (DX suite AC14-AC15)."""

from __future__ import annotations

import asyncio

import pytest

from ontary.authoring import Ontology, OntologyObject, prop
from ontary.client import OntologyClient, OntologyRuntime
from ontary.errors import VisibilityError
from ontary.mcp_server import build_mcp_server
from ontary.meta import Cardinality, Sensitivity
from ontary.scope import DirectProperty, SelfScope, ViaLink
from ontary.security import Consumer, ConsumerKind
from ontary.store import Source
from ontary.store.inmemory import InMemoryStore

SRC = Source(source_system="explain-test")


def _consumer(
    *,
    scope_level: str = "team",
    scope_id: str = "team-1",
    kind: ConsumerKind = "human",
) -> Consumer:
    return Consumer(
        actor_id="operator-subject",
        role="Agent",
        scope_level=scope_level,
        scope_id=scope_id,
        kind=kind,
    )


def _fixture() -> tuple[Ontology, OntologyRuntime, InMemoryStore]:
    ontology = Ontology(name="explain-traces", scope_levels=["team", "org"], min_n=3)

    @ontology.object(layer="L0", scope=[SelfScope(level="org")])
    class Org(OntologyObject):
        id: str = prop(primary_key=True)

    @ontology.object(
        layer="L0",
        scope=[
            SelfScope(level="team"),
            ViaLink(link_api_name="teamInOrg", direction="from", parent_type="Org"),
        ],
    )
    class Team(OntologyObject):
        id: str = prop(primary_key=True)

    @ontology.object(
        layer="L0",
        scope=[
            DirectProperty(level="team", property_name="team_id"),
            ViaLink(link_api_name="ticketInTeam", direction="from", parent_type="Team"),
        ],
    )
    class Ticket(OntologyObject):
        id: str = prop(primary_key=True)
        team_id: str | None = prop(default=None, scope_level="team")
        title: str
        agent_secret: str | None = prop(
            default=None,
            sensitivity=Sensitivity(ai_usable=False, human_visible=True),
        )

    @ontology.object(
        layer="L0",
        scope=[DirectProperty(level="team", property_name="team_id")],
        contributor=[DirectProperty(level="identity", property_name="person_id")],
    )
    class Response(OntologyObject):
        id: str = prop(primary_key=True)
        team_id: str = prop(scope_level="team")
        person_id: str
        value: float

    ontology.link("teamInOrg", Team, Org, Cardinality.MANY_TO_ONE)
    ontology.link("ticketInTeam", Ticket, Team, Cardinality.MANY_TO_ONE)
    ontology.validate()

    store = InMemoryStore(ontology.registry)
    store.insert("Org", {"id": "org-1"}, SRC)
    store.insert("Team", {"id": "team-1"}, SRC)
    store.insert("Team", {"id": "team-2"}, SRC)
    store.create_link("teamInOrg", "team-1", "org-1")
    store.create_link("teamInOrg", "team-2", "org-1")
    store.insert(
        "Ticket",
        {
            "id": "ticket-1",
            "team_id": "team-1",
            "title": "Visible ticket",
            "agent_secret": "human-only",
        },
        SRC,
    )
    store.create_link("ticketInTeam", "ticket-1", "team-1")
    store.insert(
        "Ticket",
        {"id": "ticket-2", "team_id": "team-2", "title": "Hidden ticket"},
        SRC,
    )
    store.create_link("ticketInTeam", "ticket-2", "team-2")
    for response_id, person_id in (("response-1", "person-1"), ("response-2", "person-2")):
        store.insert(
            "Response",
            {
                "id": response_id,
                "team_id": "team-1",
                "person_id": person_id,
                "value": 4.0,
            },
            SRC,
        )

    return ontology, ontology.bind(store), store


def test_direct_property_trace_records_match_and_level_mismatch() -> None:
    _ontology, runtime, _store = _fixture()

    trace = runtime.explain_read(_consumer(), "Ticket", "ticket-1")

    direct = [entry for entry in trace.rules if entry.rule_kind == "DirectProperty"]
    assert any(
        entry.requested_level == "team" and entry.matched and entry.resolved_scope_id == "team-1"
        for entry in direct
    )
    assert any(entry.requested_level == "org" and not entry.matched for entry in direct)
    assert trace.verdict == "visible"
    assert trace.error_code is None


def test_via_link_trace_shows_the_resolved_hop() -> None:
    _ontology, runtime, _store = _fixture()

    trace = runtime.explain_read(
        _consumer(scope_level="org", scope_id="org-1"), "Ticket", "ticket-1"
    )

    via = next(
        entry
        for entry in trace.rules
        if entry.rule_kind == "ViaLink"
        and entry.link_api_name == "ticketInTeam"
        and entry.requested_level == "org"
        and entry.matched
    )
    assert [(step.object_type, step.object_id) for step in via.scope_path] == [
        ("Ticket", "ticket-1"),
        ("Team", "team-1"),
        ("Org", "org-1"),
    ]
    assert [step.via_link for step in via.scope_path[1:]] == [
        "ticketInTeam",
        "teamInOrg",
    ]


def test_self_scope_rule_is_traced() -> None:
    _ontology, runtime, _store = _fixture()

    trace = runtime.explain_read(_consumer(), "Team", "team-1")

    assert any(
        entry.rule_kind == "SelfScope"
        and entry.requested_level == "team"
        and entry.matched
        and entry.resolved_scope_id == "team-1"
        for entry in trace.rules
    )


def test_ai_sensitivity_redaction_names_the_field_and_consumer_kind() -> None:
    _ontology, runtime, _store = _fixture()

    trace = runtime.explain_read(_consumer(kind="ai"), "Ticket", "ticket-1")

    assert trace.verdict == "redacted"
    assert len(trace.redactions) == 1
    assert trace.redactions[0].consumer_kind == "ai"
    assert trace.redactions[0].fields == ("agent_secret",)


def test_explain_list_pins_failed_min_n_for_the_selected_population() -> None:
    _ontology, runtime, _store = _fixture()

    traces = runtime.explain_list(_consumer(), "Response", where={"team_id": "team-1"})

    assert [trace.object_id for trace in traces] == ["response-1", "response-2"]
    assert all(trace.min_n.outcome == "failed" for trace in traces)
    assert all(trace.min_n.threshold == 3 for trace in traces)
    assert all(trace.min_n.count == 2 for trace in traces)


def test_nonexistent_id_returns_not_found_trace_with_same_shape() -> None:
    _ontology, runtime, _store = _fixture()

    trace = runtime.explain_read(_consumer(), "Ticket", "does-not-exist")

    assert trace.verdict == "not_found"
    assert trace.error_code is None
    assert trace.rules == ()
    assert trace.redactions == ()
    assert trace.min_n.outcome == "not_applicable"


def test_denied_trace_error_code_matches_real_client_get() -> None:
    _ontology, runtime, _store = _fixture()
    consumer = _consumer()
    client = runtime.for_consumer(consumer)

    trace = runtime.explain_read(consumer, "Ticket", "ticket-2")
    with pytest.raises(VisibilityError) as excinfo:
        client.get("Ticket", "ticket-2")

    assert trace.verdict == "denied"
    assert trace.error_code == excinfo.value.code


def test_explain_is_absent_from_consumer_client() -> None:
    _ontology, runtime, _store = _fixture()
    client = runtime.for_consumer(_consumer())

    assert not hasattr(client, "explain_read")
    assert not hasattr(client, "explain_list")
    assert not hasattr(client, "explain_scan")


def test_explain_is_not_registered_as_an_mcp_tool() -> None:
    ontology, _runtime, store = _fixture()
    server = build_mcp_server(ontology, store, _consumer())

    tool_names = {tool.name for tool in asyncio.run(server.list_tools())}
    assert "explain_scan" not in tool_names
    assert not any("explain" in name.lower() for name in tool_names)


def test_explicit_none_collector_is_byte_identical_to_plain_reads() -> None:
    _ontology, runtime, _store = _fixture()
    consumer = _consumer(kind="ai")

    plain_get = runtime.query.get_object(consumer, "Ticket", "ticket-1")
    none_get = runtime.query.get_object(consumer, "Ticket", "ticket-1", trace=None)
    plain_list = runtime.query.get_objects(consumer, "Ticket", limit=None)
    none_list = runtime.query.get_objects(
        consumer, "Ticket", limit=None, trace=None
    )

    assert plain_get is not None
    assert none_get is not None
    assert plain_get.model_dump_json() == none_get.model_dump_json()
    assert [row.model_dump_json() for row in plain_list] == [
        row.model_dump_json() for row in none_list
    ]


def test_explain_models_are_frozen() -> None:
    _ontology, runtime, _store = _fixture()
    trace = runtime.explain_read(_consumer(), "Ticket", "ticket-1")

    with pytest.raises(Exception):
        trace.verdict = "denied"  # type: ignore[misc]

    report = runtime.explain_scan(_consumer(), "Ticket")
    with pytest.raises(Exception):
        report.rows_scanned = 0  # type: ignore[misc]


def test_runtime_methods_return_canonical_decision_trace_model() -> None:
    from ontary.explain import DecisionTrace, ScanReport

    _ontology, runtime, _store = _fixture()

    assert isinstance(runtime.explain_read(_consumer(), "Ticket", "ticket-1"), DecisionTrace)
    assert all(
        isinstance(trace, DecisionTrace) for trace in runtime.explain_list(_consumer(), "Ticket")
    )
    assert isinstance(runtime.explain_scan(_consumer(), "Ticket"), ScanReport)


def test_runtime_only_boundary_holds_for_directly_constructed_client() -> None:
    ontology, _runtime, store = _fixture()
    client = OntologyClient(ontology, store, _consumer())

    assert not hasattr(client, "explain_read")
    assert not hasattr(client, "explain_list")
    assert not hasattr(client, "explain_scan")
