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
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
from mcp.server.fastmcp import FastMCP

from examples.tickets import run_mcp
from examples.tickets.fixtures import load_fixtures
from examples.tickets.ontology import build_ontology
from ontary import ActionContext, ActionParams, BoundQuery, Ontology, OntologyObject, prop, target
from ontary.actions import ActionError
from ontary.client import OntologyClient
from ontary.errors import VisibilityError
from ontary.mcp_server import build_mcp_server
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
    assert {t.payload["id"] for t in tickets} == {
        ids[f"ticket_{number}_id"] for number in (1, 2, 4, 5, 6, 7, 8, 9, 10)
    }

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
    assert mean_age == 127.0 / 9


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


def test_ticket_stats_aggregates_above_min_n() -> None:
    # `ScopePolicy.min_n=2`; queue_b has seven varied tickets, so the app's
    # Function releases the exact mean rather than exercising only refusal.
    ontology, store = build_ontology()
    ids = load_fixtures(store)
    client = OntologyClient(ontology, store, _agent(ids["queue_b_id"]))

    assert client.call_function("ticketStats", {"queue_id": ids["queue_b_id"]}) == 115.0 / 7


def test_ticket_aggregate_enforces_min_n() -> None:
    ontology, store = build_ontology()
    ids = load_fixtures(store)
    client = OntologyClient(ontology, store, _agent(ids["queue_b_id"]))

    selected = client.list(
        "Ticket",
        where={"subject": {"contains": "callback"}},
        limit=None,
    )
    assert len(selected) == 1, "the min-N refusal must not pass on an empty selection"

    with pytest.raises(VisibilityError) as exc_info:
        client.aggregate(
            "Ticket",
            "age_hours",
            where={"subject": {"contains": "callback"}},
        )

    assert exc_info.value.code == "MIN_N_VIOLATION"


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
    assert names == {"Org", "Queue", "Agent", "Ticket", "Comment", "Escalation"}


def test_mcp_query_objects() -> None:
    ontology, store = build_ontology()
    ids = load_fixtures(store)
    server = build_mcp_server(
        ontology,
        store,
        _agent(ids["queue_a_id"]),
    )
    payload = _call(server, "query_objects", {"obj_type": "Ticket"})
    assert {r["payload"]["id"] for r in payload["result"]} == {
        ids[f"ticket_{number}_id"] for number in (1, 2, 4, 5, 6, 7, 8, 9, 10)
    }


def test_mcp_execute_action_success_and_denial() -> None:
    ontology, store = build_ontology()
    ids = load_fixtures(store)

    server = build_mcp_server(
        ontology,
        store,
        _agent(ids["queue_a_id"]),
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
    assert payload["result"] == 127.0 / 9


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


# -- AC15: runnable examples -------------------------------------------


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
