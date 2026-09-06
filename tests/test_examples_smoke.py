"""AC11 smoke: `examples/tickets` plus a small inline ontology
-- pass the same client + MCP smoke tests, proving the engine holds zero
domain assumptions.

Covers:
- build -> validate() -> ingest fixtures -> `OntologyClient` get/list/
  traverse/execute/call_function, plus one scope denial.
- `build_mcp_server` over the same ontology, exercised in-process (no
  network/subprocess) for a representative tool subset: list_object_types,
  query_objects, execute_action (success + a structured denial), and
  call_function.
- two-ontology coexistence: the inline ontology builds + validates alongside
  `examples/tickets`'s in the same process, with no cross-talk between their
  registries, actions, or functions (spec §7).
- `examples.tickets.connector` -- the AC11 second-domain connector -- landing
  inline raw fixtures through `run_pipeline` (objects+links, clean report,
  idempotent re-run).
- `examples/tickets/run_connector.py` (spec AC15 runnable example) -- its
  `main()` entrypoint runs without raising and prints a recognizable
  `RunReport` summary.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from datetime import datetime, timezone
from typing import Any

import pytest
from mcp.server.fastmcp import FastMCP

from examples.tickets import run_effects, run_mcp
from examples.tickets.connector import TicketsCSVLikeSource, build_tickets_mapping_spec
from examples.tickets.fixtures import load_fixtures
from examples.tickets.ontology import NOTIFY_TICKET_ESCALATION, build_ontology
from ontary import ActionContext, ActionParams, BoundQuery, Ontology, OntologyObject, prop, target
from ontary.actions import ActionError
from ontary.client import OntologyClient
from ontary.connect import oid, run_pipeline
from ontary.effects import EffectMeta, EffectPayload
from ontary.errors import VisibilityError
from ontary.mcp_server import build_mcp_server
from ontary.outbox import DrainReport
from ontary.security import Consumer
from ontary.store import ObjectStore

_INLINE_ONTOLOGY = Ontology(name="coexistence", scope_levels=["org"], min_n=1)


@_INLINE_ONTOLOGY.object(layer="L0", scope="unscoped")
class InlineNote(OntologyObject):
    id: str = prop(primary_key=True)
    body: str


class ArchiveInlineNoteParams(ActionParams):
    note_id: str = target(InlineNote)


@_INLINE_ONTOLOGY.action(
    ArchiveInlineNoteParams,
    target=InlineNote,
    roles=["Operator"],
    api_name="ArchiveInlineNote",
)
def _archive_inline_note(
    _ctx: ActionContext, params: ArchiveInlineNoteParams
) -> dict[str, str]:
    return {"note_id": params.note_id}


@_INLINE_ONTOLOGY.function(api_name="inlineNoteStats")
def _inline_note_stats(_query: BoundQuery, _params: dict[str, Any]) -> float:
    return 0.0


_INLINE_ONTOLOGY.validate()


def _build_inline_ontology() -> tuple[Ontology, ObjectStore]:
    return _INLINE_ONTOLOGY, ObjectStore(_INLINE_ONTOLOGY.registry)


def _agent(scope_id: str) -> Consumer:
    return Consumer(
        actor_id="agent-1", role="Agent", scope_level="queue", scope_id=scope_id, kind="human"
    )


def _ai_agent(scope_id: str) -> Consumer:
    return Consumer(
        actor_id="agent-ai", role="Agent", scope_level="queue", scope_id=scope_id, kind="ai"
    )


_RECORDED_TICKET_EFFECTS: list[EffectPayload] = []


def _record_ticket_effect(payload: EffectPayload, _meta: EffectMeta) -> None:
    _RECORDED_TICKET_EFFECTS.append(payload)


def _seeded() -> tuple[OntologyClient, dict[str, str], ObjectStore]:
    """Build the tickets ontology + fixtures and return an `OntologyClient`
    bound to an Agent scoped to the seeded `queue_a` -- `EscalateTicket`/
    `ticketStats` handlers auto-bind from the declare-once `Ontology`."""
    ontology, store = build_ontology()
    ids = load_fixtures(store)
    client = OntologyClient(
        ontology,
        store,
        _agent(ids["queue_a_id"]),
        effects={NOTIFY_TICKET_ESCALATION: _record_ticket_effect},
    )
    return client, ids, store


def test_tickets_ontology_validates() -> None:
    ontology, _ = build_ontology()
    assert ontology.name == "tickets"


def test_client_get_list_traverse() -> None:
    client, ids, _ = _seeded()

    ticket = client.get("Ticket", ids["ticket_1_id"])
    assert ticket is not None
    assert ticket.payload["subject"] == "Invoice mismatch"

    tickets = client.list(
        "Ticket", where={"queue_id": ids["queue_a_id"]}, limit=None
    )
    assert {t.payload["id"] for t in tickets} == {ids["ticket_1_id"], ids["ticket_2_id"]}

    parent_tickets = client.traverse("Comment", "commentOnTicket", ids["comment_id"])
    assert [t.payload["id"] for t in parent_tickets] == [ids["ticket_1_id"]]


def test_client_execute_and_call_function() -> None:
    client, ids, _ = _seeded()

    result = client.execute("EscalateTicket", {"ticket_id": ids["ticket_1_id"]})
    assert result == {"ticket_id": ids["ticket_1_id"]}
    ticket = client.get("Ticket", ids["ticket_1_id"])
    assert ticket is not None
    assert ticket.payload["escalated"] is True
    assert ticket.payload["status"] == "open"

    mean_age = client.call_function("ticketStats", {"queue_id": ids["queue_a_id"]})
    assert mean_age == (4.0 + 12.0) / 2


def test_client_denies_out_of_scope_read() -> None:
    ontology, store = build_ontology()
    ids = load_fixtures(store)
    # scoped to queue_b, reading a ticket that lives in queue_a.
    client = OntologyClient(ontology, store, _agent(ids["queue_b_id"]))

    try:
        client.get("Ticket", ids["ticket_1_id"])
    except VisibilityError as exc:
        assert exc.code == "VISIBILITY_DENIED"
    else:
        raise AssertionError("expected VISIBILITY_DENIED for an out-of-scope queue read")


def test_traverse_denies_identity_revealing_link_for_human_but_not_ai() -> None:
    # `commentByAgent` is declared `identity_revealing=True` -- a human
    # consumer traversing Comment -> Agent would re-identify who authored a
    # (possibly scoped/redacted) comment, so the query layer must deny it
    # before an AI consumer with the identical scope is let through.
    ontology, store = build_ontology()
    ids = load_fixtures(store)

    human_client = OntologyClient(ontology, store, _agent(ids["queue_a_id"]))
    try:
        human_client.traverse("Comment", "commentByAgent", ids["comment_id"])
    except VisibilityError as exc:
        assert exc.code == "VISIBILITY_DENIED"
    else:
        raise AssertionError(
            "expected VISIBILITY_DENIED for a human traversing commentByAgent"
        )

    ai_client = OntologyClient(ontology, store, _ai_agent(ids["queue_a_id"]))
    authors = ai_client.traverse("Comment", "commentByAgent", ids["comment_id"])
    assert [a.payload["id"] for a in authors] == [ids["agent_1_id"]]


def test_ticket_stats_enforces_min_n() -> None:
    # `ScopePolicy.min_n=2`, and `queue_b` was seeded with only one ticket
    # (`ticket_3_id`) -- aggregating over it must raise MIN_N_VIOLATION,
    # regardless of whether it's reached via `call_function` or the
    # `ticketStats` handler's own `query.aggregate` call.
    ontology, store = build_ontology()
    ids = load_fixtures(store)
    client = OntologyClient(ontology, store, _agent(ids["queue_b_id"]))

    try:
        client.call_function("ticketStats", {"queue_id": ids["queue_b_id"]})
    except VisibilityError as exc:
        assert exc.code == "MIN_N_VIOLATION"
    else:
        raise AssertionError(
            "expected MIN_N_VIOLATION aggregating a single-ticket queue "
            "(min_n=2)"
        )


def test_agent_email_hidden_from_human_visible_to_ai() -> None:
    # `Agent.email` is declared `Sensitivity(human_visible=False)`; `Agent`
    # is an unscoped type, so any consumer can read it -- the redaction
    # itself is what's under test.
    ontology, store = build_ontology()
    ids = load_fixtures(store)

    human_client = OntologyClient(ontology, store, _agent(ids["queue_a_id"]))
    agent_for_human = human_client.get("Agent", ids["agent_1_id"])
    assert agent_for_human is not None
    assert "email" not in agent_for_human.payload

    ai_client = OntologyClient(ontology, store, _ai_agent(ids["queue_a_id"]))
    agent_for_ai = ai_client.get("Agent", ids["agent_1_id"])
    assert agent_for_ai is not None
    assert agent_for_ai.payload["email"] == "ada@acme.test"


# -- MCP smoke ----------------------------------------------------------


def _call(server: FastMCP, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    result = asyncio.run(server.call_tool(name, arguments))
    if isinstance(result, tuple):
        _content, structured = result
        assert isinstance(structured, dict)
        return structured
    payload: dict[str, Any] = json.loads(result[0].text)  # type: ignore[union-attr]
    return payload


def test_mcp_list_object_types() -> None:
    ontology, store = build_ontology()
    load_fixtures(store)
    server = build_mcp_server(ontology, store, _agent("queue-any"))
    payload = _call(server, "list_object_types", {})
    names = {d["api_name"] for d in payload["object_types"]}
    assert names == {"Org", "Queue", "Agent", "Ticket", "Comment"}


def test_mcp_query_objects() -> None:
    ontology, store = build_ontology()
    ids = load_fixtures(store)
    server = build_mcp_server(
        ontology,
        store,
        _agent(ids["queue_a_id"]),
        effects={NOTIFY_TICKET_ESCALATION: _record_ticket_effect},
    )
    payload = _call(server, "query_objects", {"obj_type": "Ticket"})
    assert {r["payload"]["id"] for r in payload["result"]} == {
        ids["ticket_1_id"],
        ids["ticket_2_id"],
    }


def test_mcp_execute_action_success_and_denial() -> None:
    ontology, store = build_ontology()
    ids = load_fixtures(store)

    server = build_mcp_server(
        ontology,
        store,
        _agent(ids["queue_a_id"]),
        effects={NOTIFY_TICKET_ESCALATION: _record_ticket_effect},
    )
    payload = _call(
        server,
        "execute_action",
        {"api_name": "EscalateTicket", "params": {"ticket_id": ids["ticket_1_id"]}},
    )
    assert payload == {"result": {"ticket_id": ids["ticket_1_id"]}}

    # An Agent scoped to queue_b has no scope over queue_a's ticket.
    server_denied = build_mcp_server(
        ontology,
        store,
        _agent(ids["queue_b_id"]),
        effects={NOTIFY_TICKET_ESCALATION: _record_ticket_effect},
    )
    denial_payload = _call(
        server_denied,
        "execute_action",
        {"api_name": "EscalateTicket", "params": {"ticket_id": ids["ticket_2_id"]}},
    )
    assert "error" in denial_payload
    assert "result" not in denial_payload


def test_mcp_call_function() -> None:
    ontology, store = build_ontology()
    ids = load_fixtures(store)
    server = build_mcp_server(ontology, store, _agent(ids["queue_a_id"]))
    payload = _call(
        server, "call_function", {"api_name": "ticketStats", "params": {"queue_id": ids["queue_a_id"]}}
    )
    assert payload["result"] == (4.0 + 12.0) / 2


# -- two-ontology coexistence ---------------------------------------------


def test_two_ontologies_coexist_in_one_process_without_cross_talk() -> None:
    """Two ontologies loaded into one process must stay completely separate.

    The registry identity, disjoint object types, and positive/negative
    declaration lookups ensure this catches shared registry state rather than
    merely comparing two sets of constants.
    """
    inline_ontology, _inline_store = _build_inline_ontology()
    tickets_ontology, tickets_store = build_ontology()

    assert inline_ontology.name == "coexistence"
    assert tickets_ontology.name == "tickets"
    assert inline_ontology.registry is not tickets_ontology.registry
    assert set(inline_ontology.registry.object_types).isdisjoint(
        tickets_ontology.registry.object_types
    )

    # Both declarations exist, but neither ontology can see the other's
    # actions or functions.
    assert "ArchiveInlineNote" in inline_ontology.registry.action_types
    assert "EscalateTicket" in tickets_ontology.registry.action_types
    assert "EscalateTicket" not in inline_ontology.registry.action_types
    assert "ArchiveInlineNote" not in tickets_ontology.registry.action_types
    assert "inlineNoteStats" in inline_ontology.registry.functions
    assert "ticketStats" in tickets_ontology.registry.functions
    assert "ticketStats" not in inline_ontology.registry.functions
    assert "inlineNoteStats" not in tickets_ontology.registry.functions

    # And a client bound to one refuses the other's action by name.
    client = OntologyClient(
        tickets_ontology,
        tickets_store,
        Consumer(actor_id="a", role="Agent", scope_level="queue", scope_id="q", kind="human"),
    )
    with pytest.raises(ActionError) as caught:
        client.execute("ArchiveInlineNote", {"note_id": "x"})
    assert caught.value.code == "UNKNOWN_ACTION"


# -- AC11: examples/tickets/connector.py -----------------------------------

_TICKETS_RAW = {
    "orgs": [{"org_id": "o1", "name": "Acme Support"}],
    "queues": [{"queue_id": "q1", "org_id": "o1", "name": "Billing"}],
    "agents": [{"agent_id": "a1", "display_name": "Ada", "email": "ada@acme.test"}],
    "tickets": [
        {"ticket_id": "t1", "queue_id": "q1", "subject": "Invoice mismatch", "age_hours": 4.0},
        {"ticket_id": "t2", "queue_id": "q1", "subject": "Refund request", "age_hours": 12.0},
    ],
}


def test_tickets_connector_ingest_smoke() -> None:
    """AC11: a tiny non-DSO connector, own canonical schema + `MappingSpec`,
    landing inline raw fixtures through `run_pipeline` -- objects+links land,
    the report is clean, and re-running the same batch is idempotent (no
    duplicate objects/links, report counts stable)."""
    ontology, store = build_ontology()
    connector = TicketsCSVLikeSource(extracted_at=datetime(2026, 7, 23, tzinfo=timezone.utc))
    mapping = build_tickets_mapping_spec()

    report = run_pipeline(connector, mapping, ontology, store, raw=_TICKETS_RAW, run_at="run-1")

    assert report.ok
    assert report.written == {"Org": 1, "Queue": 1, "Agent": 1, "Ticket": 2}
    assert report.links_created == {"queueOfOrg": 1, "ticketInQueue": 2}
    assert report.links_skipped.get("queueOfOrg", 0) == 0
    assert report.links_skipped.get("ticketInQueue", 0) == 0

    org_id = oid("tickets_csv", "Org", "o1")
    ticket_1_id = oid("tickets_csv", "Ticket", "t1")
    ticket_2_id = oid("tickets_csv", "Ticket", "t2")
    org = store.read_current("Org", org_id)
    assert org is not None
    assert org.payload["name"] == "Acme Support"
    t1 = store.read_current("Ticket", ticket_1_id)
    t2 = store.read_current("Ticket", ticket_2_id)
    assert t1 is not None and t1.payload["subject"] == "Invoice mismatch"
    assert t2 is not None and t2.payload["subject"] == "Refund request"

    # Re-running the identical batch is idempotent: same oids -> upsert, no
    # duplicate objects, and the already-linked pairs are skipped rather
    # than re-created (spec AC4/AC11).
    report2 = run_pipeline(connector, mapping, ontology, store, raw=_TICKETS_RAW, run_at="run-2")
    assert report2.ok
    assert report2.written == {"Org": 1, "Queue": 1, "Agent": 1, "Ticket": 2}
    assert store.read_current("Org", org_id) is not None
    assert report2.links_created == {"queueOfOrg": 0, "ticketInQueue": 0}
    assert report2.links_skipped == {"queueOfOrg": 0, "ticketInQueue": 0}


# -- AC15: runnable examples -------------------------------------------


def test_run_connector_example_prints_run_report_and_exits_zero() -> None:
    """`examples/tickets/run_connector.py` is the AC15 runnable connector
    example -- run it exactly as a user would (`python -m
    examples.tickets.run_connector`) and check it succeeds and prints a
    recognizable `RunReport` summary."""
    result = subprocess.run(
        [sys.executable, "-m", "examples.tickets.run_connector"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "ok=True" in result.stdout
    assert "written={'Org': 1, 'Queue': 1, 'Agent': 1, 'Ticket': 2}" in result.stdout
    assert "re-run ok=True" in result.stdout


def test_run_mcp_example_builds_server_and_returns_query_envelope() -> None:
    """The MCP entrypoint exposes the real tickets tools and guarded data."""
    server = run_mcp.build_server()

    tools = asyncio.run(server.list_tools())
    assert {tool.name for tool in tools} == {
        "list_object_types",
        "list_link_types",
        "list_action_types",
        "list_functions",
        "get_declarations",
        "get_object",
        "query_objects",
        "count_objects",
        "aggregate_objects",
        "traverse_links",
        "execute_action",
        "call_function",
    }

    payload = _call(server, "query_objects", {"obj_type": "Ticket", "limit": 1})
    assert set(payload) == {"result", "next_cursor"}
    assert len(payload["result"]) == 1
    assert payload["result"][0]["payload"]["subject"] in {
        "Invoice mismatch",
        "Refund request",
    }
    assert payload["next_cursor"] is not None


def test_run_effects_example_dispatches_effect_and_reports_drain(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The effects entrypoint performs the action and reports the drain."""
    report = run_effects.run()
    # The printing dispatcher succeeds during the action's inline attempt, so
    # the subsequent drain has a complete, zero-row accounting report.
    assert report == DrainReport(claimed=0)
    output = capsys.readouterr().out
    assert "NotifyTicketEscalation" in output
    assert "payload={'ticket_id':" in output
    assert "'reason': 'priority'" in output
    assert "claimed=0 delivered=0 retrying=0 failed=0 skipped=0" in output
