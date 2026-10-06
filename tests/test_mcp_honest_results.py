"""Exact MCP read shapes and declaration-only marks (#62, T2)."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
from mcp.server.mcpserver import MCPServer

from ontary.authoring import Ontology, OntologyObject, prop
from ontary.client import OntologyClient
from ontary.errors import OntaryError, ValidationFailed
from ontary.mcp_server import _register_tools, build_mcp_server
from ontary.meta import Cardinality, Sensitivity
from ontary.scope import DirectProperty, RowVisibilityStore, SelfScope, ViaLink
from ontary.security import Consumer, ConsumerKind
from ontary.store import ObjectStore, Source

SRC = Source(source_system="test")
READ_TOOLS = ("get_object", "query_objects", "count_objects", "traverse_links")
LINEAGE_KEYS = {
    "object_type", "object_id", "valid_from", "valid_to",
    "source_system", "source_id", "extracted_at",
}


def _call(server: MCPServer, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    result = asyncio.run(server.call_tool(name, arguments))
    if result.structured_content is not None:
        return result.structured_content
    payload: dict[str, Any] = json.loads(result.content[0].text)
    return payload


def _consumer(kind: ConsumerKind = "human") -> Consumer:
    return Consumer(actor_id="reader", role="Reader", scope_level="team", scope_id="a", kind=kind)


def _row_visible(
    _store: RowVisibilityStore,
    _consumer: Consumer,
    _obj_type: str,
    payload: dict[str, Any],
) -> bool:
    return not payload["blocked"]


def _ontology(
    mode: str = "scoped", *, hidden_routing: bool = False, identity_link: bool = False
) -> Ontology:
    ontology = Ontology("mcp-read-marks", scope_levels=["team"])

    @ontology.object(layer="L0", scope="unscoped")
    class Team(OntologyObject):
        id: str = prop(primary_key=True)

    @ontology.object(
        layer="L0",
        scope=[DirectProperty(level="team", property_name="team_id")]
        if mode == "scoped" else "unscoped",
        row_visibility=_row_visible if mode == "row-rule" else None,
    )
    class Ticket(OntologyObject):
        id: str = prop(primary_key=True)
        team_id: str | None = prop(
            default=None,
            sensitivity=Sensitivity(human_visible=not hidden_routing, ai_usable=not hidden_routing),
        )
        title: str
        blocked: bool
        internal_note: str | None = prop(default=None, sensitivity=Sensitivity(human_visible=False))
        ai_signal: str | None = prop(default=None, sensitivity=Sensitivity(ai_usable=False))
        sparse_secret: str | None = prop(
            default=None, sensitivity=Sensitivity(human_visible=False, ai_usable=False)
        )

    ontology.link("inTeam", Ticket, Team, Cardinality.MANY_TO_ONE, identity_revealing=identity_link)
    ontology.validate()
    return ontology


def _insert_ticket(store: ObjectStore, obj_id: str, *, hidden: bool = False) -> None:
    store.insert("Ticket", {
        "id": obj_id, "team_id": "b" if hidden else "a", "title": obj_id,
        "blocked": hidden, "internal_note": "human secret", "ai_signal": "ai secret",
        # sparse_secret is deliberately never stored: marks must follow declarations.
    }, SRC)
    store.create_link("inTeam", obj_id, "a")


def _server(ontology: Ontology, store: ObjectStore, kind: ConsumerKind = "human") -> tuple[
    MCPServer, OntologyClient
]:
    client = OntologyClient(ontology, store, _consumer(kind))
    server = MCPServer("read-marks")
    _register_tools(server, ontology.definition, lambda: client)
    return server, client


def _arguments(tool: str) -> dict[str, Any]:
    return {
        "get_object": {"obj_type": "Ticket", "obj_id": "t-1"},
        "query_objects": {"obj_type": "Ticket", "limit": 3},
        "count_objects": {"obj_type": "Ticket"},
        "traverse_links": {
            "obj_type": "Team", "obj_id": "a", "link_api_name": "inTeam", "reverse": True,
        },
    }[tool].copy()


def _assert_row(
    row: dict[str, Any], obj_type: str, payload: dict[str, Any], redacted: list[str]
) -> None:
    assert set(row) == {"payload", "lineage", "redacted_fields"}
    assert row["payload"] == payload
    assert row["redacted_fields"] == redacted
    assert all(key not in row["payload"] for key in redacted)
    assert set(row["lineage"]) == LINEAGE_KEYS
    assert row["lineage"]["object_type"] == obj_type
    assert row["lineage"]["object_id"] == payload["id"]
    assert row["lineage"]["source_system"] == "test"
    assert isinstance(row["lineage"]["valid_from"], str)
    assert row["lineage"]["valid_to"] is None


def _assert_ticket_read(
    result: dict[str, Any], tool: str, ids: list[str], *, limited: bool,
    kind: ConsumerKind = "human", hidden_routing: bool = False,
) -> None:
    keys = {"result"}
    if tool != "get_object":
        keys.add("scope_limited")
        assert result["scope_limited"] is limited
    if tool == "query_objects":
        keys.add("next_cursor")
        assert result["next_cursor"] is None
    assert set(result) == keys
    if tool == "count_objects":
        assert type(result["result"]) is int
        assert result["result"] == len(ids)
        return
    rows = [result["result"]] if tool == "get_object" else result["result"]
    assert isinstance(rows, list)
    assert [row["payload"]["id"] for row in rows] == ids
    redacted = ["internal_note", "sparse_secret"] if kind == "human" else ["ai_signal", "sparse_secret"]
    if hidden_routing:
        redacted.append("team_id")
    for row, obj_id in zip(rows, ids, strict=True):
        payload = {"id": obj_id, "title": obj_id, "blocked": False}
        payload["ai_signal" if kind == "human" else "internal_note"] = (
            "ai secret" if kind == "human" else "human secret"
        )
        if not hidden_routing:
            payload["team_id"] = "a"
        _assert_row(row, "Ticket", payload, redacted)


def test_golden_sample_scoped_human_ticket_page() -> None:
    ontology = Ontology("golden", scope_levels=["team"])

    @ontology.object(layer="L0", scope=[SelfScope(level="team")])
    class Team(OntologyObject):
        id: str = prop(primary_key=True)

    @ontology.object(layer="L0", scope=[ViaLink(
        link_api_name="inTeam", direction="from", parent_type="Team",
    )])
    class Ticket(OntologyObject):
        id: str = prop(primary_key=True)
        title: str
        status: str
        internal_note: str | None = prop(default=None, sensitivity=Sensitivity(human_visible=False))

    ontology.link("inTeam", Ticket, Team, Cardinality.MANY_TO_ONE)
    ontology.validate()
    store = ObjectStore(ontology.registry)
    for team in ("a", "b"):
        store.insert("Team", {"id": team}, SRC)
    for obj_id, title, team in (
        ("t-1", "Printer jam", "a"), ("t-2", "Hidden ticket", "b"),
        ("t-4", "VPN down", "a"), ("t-9", "Next page", "a"),
    ):
        store.insert("Ticket", {
            "id": obj_id, "title": title, "status": "open", "internal_note": "secret",
        }, SRC)
        store.create_link("inTeam", obj_id, team)
    server = build_mcp_server(ontology, store, _consumer())

    result = _call(server, "query_objects", {"obj_type": "Ticket", "limit": 2})

    assert set(result) == {"result", "next_cursor", "scope_limited"}
    assert result["scope_limited"] is True
    assert isinstance(result["next_cursor"], str) and result["next_cursor"]
    assert len(result["result"]) == 2
    for row, obj_id, title in zip(
        result["result"], ("t-1", "t-4"), ("Printer jam", "VPN down"), strict=True,
    ):
        _assert_row(row, "Ticket", {"id": obj_id, "title": title, "status": "open"}, ["internal_note"])


@pytest.mark.parametrize("tool", READ_TOOLS)
@pytest.mark.parametrize("kind", ["human", "ai"])
@pytest.mark.parametrize("hidden_routing", [False, True])
def test_read_shapes_and_declared_redaction(
    tool: str, kind: ConsumerKind, hidden_routing: bool,
) -> None:
    ontology = _ontology(hidden_routing=hidden_routing)
    store = ObjectStore(ontology.registry)
    store.insert("Team", {"id": "a"}, SRC)
    for obj_id in ("t-1", "t-4"):
        _insert_ticket(store, obj_id)
    _insert_ticket(store, "t-2", hidden=True)
    server, _ = _server(ontology, store, kind)

    _assert_ticket_read(
        _call(server, tool, _arguments(tool)), tool,
        ["t-1"] if tool == "get_object" else ["t-1", "t-4"],
        limited=True, kind=kind, hidden_routing=hidden_routing,
    )


@pytest.mark.parametrize("tool", ["query_objects", "count_objects", "traverse_links"])
@pytest.mark.parametrize("mode", ["scoped", "unscoped", "row-rule"])
@pytest.mark.parametrize("kind", ["human", "ai"])
@pytest.mark.parametrize("empty", [False, True])
def test_scope_limited_is_declaration_only_across_stores(
    tool: str, mode: str, kind: ConsumerKind, empty: bool,
) -> None:
    ontology = _ontology(mode)
    results = []
    for add_hidden in (False, True):
        store = ObjectStore(ontology.registry)
        store.insert("Team", {"id": "a"}, SRC)
        if not empty:
            _insert_ticket(store, "t-1")
        # An unscoped type without a row rule cannot hide a row.
        if add_hidden and mode != "unscoped":
            _insert_ticket(store, "t-2", hidden=True)
        server, _ = _server(ontology, store, kind)
        result = _call(server, tool, _arguments(tool))
        _assert_ticket_read(
            result, tool, [] if empty else ["t-1"], limited=mode != "unscoped", kind=kind,
        )
        results.append(result)

    assert results[0]["scope_limited"] is results[1]["scope_limited"]


@pytest.mark.parametrize("mode", ["scoped", "unscoped", "row-rule"])
def test_traversal_marks_follow_forward_and_reverse_result_type(mode: str) -> None:
    ontology = _ontology(mode)
    store = ObjectStore(ontology.registry)
    store.insert("Team", {"id": "a"}, SRC)
    _insert_ticket(store, "t-1")
    server, _ = _server(ontology, store)

    forward = _call(server, "traverse_links", {
        "obj_type": "Ticket", "obj_id": "t-1", "link_api_name": "inTeam",
    })
    assert set(forward) == {"result", "scope_limited"}
    assert forward["scope_limited"] is False
    assert len(forward["result"]) == 1
    _assert_row(forward["result"][0], "Team", {"id": "a"}, [])
    reverse = _call(server, "traverse_links", _arguments("traverse_links"))
    _assert_ticket_read(reverse, "traverse_links", ["t-1"], limited=mode != "unscoped")


def _python_read(client: OntologyClient, tool: str, arguments: dict[str, Any]) -> Any:
    if tool == "get_object":
        return client.get(arguments["obj_type"], arguments["obj_id"])
    if tool == "query_objects":
        return client.list(arguments["obj_type"], arguments.get("where"), limit=arguments["limit"])
    if tool == "count_objects":
        return client.count(arguments["obj_type"], arguments.get("where"))
    return client.traverse(
        arguments["obj_type"], arguments["link_api_name"], arguments["obj_id"],
        reverse=arguments.get("reverse", False),
    )


@pytest.mark.parametrize(("tool", "failure", "code"), [
    *[(tool, "unknown-type", "UNKNOWN_NAME") for tool in READ_TOOLS],
    *[(tool, "policy", "SCOPE_POLICY_ERROR") for tool in READ_TOOLS],
    ("get_object", "invisible", "VISIBILITY_DENIED"),
    ("traverse_links", "identity-link", "VISIBILITY_DENIED"),
    ("traverse_links", "unknown-link", "UNKNOWN_NAME"),
    ("query_objects", "unknown-field", "UNKNOWN_FIELD"),
    ("count_objects", "unknown-field", "UNKNOWN_FIELD"),
])
def test_failed_reads_keep_the_error_envelope_and_never_compute_marks(
    monkeypatch: pytest.MonkeyPatch, tool: str, failure: str, code: str,
) -> None:
    ontology = _ontology(identity_link=failure == "identity-link")
    store = ObjectStore(ontology.registry)
    store.insert("Team", {"id": "a"}, SRC)
    _insert_ticket(store, "t-1")
    _insert_ticket(store, "t-2", hidden=True)
    server, client = _server(ontology, store)
    arguments = _arguments(tool)
    if failure == "unknown-type":
        arguments["obj_type"] = "Unknown"
        # Bare-string misses can return empty data; pin the error path when
        # the resolved client's read does refuse, without changing that behavior.
        def unknown_type(*args: Any, **kwargs: Any) -> Any:
            raise ValidationFailed("unknown object type 'Unknown'", code="UNKNOWN_NAME")

        method = {
            "get_object": "get", "query_objects": "list",
            "count_objects": "count", "traverse_links": "traverse",
        }[tool]
        monkeypatch.setattr(client, method, unknown_type)
    elif failure == "policy":
        ontology.definition.policy.unscoped_types.add("Ticket")
    elif failure == "invisible":
        arguments["obj_id"] = "t-2"
    elif failure == "unknown-link":
        arguments["link_api_name"] = "Unknown"
    elif failure == "unknown-field":
        arguments["where"] = {"Unknown": "x"}
    with pytest.raises(OntaryError) as caught:
        _python_read(client, tool, arguments)
    error = caught.value
    assert error.code == code
    calls = []

    def unexpected_marks(*args: Any, **kwargs: Any) -> Any:
        calls.append((args, kwargs))
        raise AssertionError("marks must not run after a refused read")

    monkeypatch.setattr(client, "_read_marks", unexpected_marks)
    monkeypatch.setattr(client, "_link_target_type", unexpected_marks)

    assert _call(server, tool, arguments) == {"error": {
        "type": type(error).__name__, "message": str(error), "code": code, "kind": error.kind,
    }}
    assert calls == []


@pytest.mark.parametrize("tool", READ_TOOLS)
def test_marks_are_computed_once_after_a_successful_read(
    monkeypatch: pytest.MonkeyPatch, tool: str,
) -> None:
    ontology = _ontology()
    store = ObjectStore(ontology.registry)
    store.insert("Team", {"id": "a"}, SRC)
    _insert_ticket(store, "t-1")
    server, client = _server(ontology, store)
    method = {
        "get_object": "get", "query_objects": "list",
        "count_objects": "count", "traverse_links": "traverse",
    }[tool]
    read = getattr(client, method)
    read_marks = client._read_marks
    link_target_type = client._link_target_type
    calls = []

    def tracked_read(*args: Any, **kwargs: Any) -> Any:
        result = read(*args, **kwargs)
        calls.append("read")
        return result

    def tracked_marks(obj_type: str) -> tuple[bool, tuple[str, ...]]:
        assert obj_type == "Ticket"
        calls.append("marks")
        return read_marks(obj_type)

    def tracked_target(link: str, *, reverse: bool) -> str:
        assert (link, reverse) == ("inTeam", True)
        calls.append("target")
        return link_target_type(link, reverse=reverse)

    monkeypatch.setattr(client, method, tracked_read)
    monkeypatch.setattr(client, "_read_marks", tracked_marks)
    monkeypatch.setattr(client, "_link_target_type", tracked_target)
    _assert_ticket_read(_call(server, tool, _arguments(tool)), tool, ["t-1"], limited=True)
    assert calls == (["read", "target", "marks"] if tool == "traverse_links" else ["read", "marks"])


@pytest.mark.parametrize("retired", [False, True])
def test_get_object_missing_or_retired_is_null_without_marks(
    monkeypatch: pytest.MonkeyPatch, retired: bool,
) -> None:
    ontology = _ontology()
    store = ObjectStore(ontology.registry)
    if retired:
        store.insert("Team", {"id": "a"}, SRC)
        _insert_ticket(store, "t-1")
        store.retire_object("Ticket", "t-1")
    server, client = _server(ontology, store)
    calls = []

    def unexpected_marks(obj_type: str) -> tuple[bool, tuple[str, ...]]:
        calls.append(obj_type)
        raise AssertionError("a null result needs no row marks")

    monkeypatch.setattr(client, "_read_marks", unexpected_marks)
    assert _call(server, "get_object", _arguments("get_object")) == {"result": None}
    assert calls == []


def test_live_read_tool_descriptions_explain_the_marks() -> None:
    ontology = _ontology()
    server, _ = _server(ontology, ObjectStore(ontology.registry))
    tools = {tool.name: tool for tool in asyncio.run(server.list_tools())}
    for name in READ_TOOLS:
        description = tools[name].description
        assert description is not None
        if name != "count_objects":
            assert "redacted_fields" in description
            assert "absent from" in description and "payload" in description
            assert "never null" in description
        if name != "get_object":
            assert "scope_limited" in description
            assert "declarations only" in description
