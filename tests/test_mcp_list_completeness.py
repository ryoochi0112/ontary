"""MCP list results say whether they are complete (#63, #64).

`query_objects` and `traverse_links` carry `has_more`, a tight `next_cursor`,
an opt-in `total`, and a count-only mode; `traverse_links` pages. Built on the
honest-results fixture: a scoped human reader of team ``a``.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from test_mcp_honest_results import (
    GOLDEN_UNKNOWN_TYPE,
    SRC,
    _call,
    _insert_ticket,
    _ontology,
    _server,
)

from ontary.mcp_server import DEFAULT_LIMIT, MAX_LIMIT
from ontary.store import ObjectStore

LIST_TOOLS = ("query_objects", "traverse_links")


def _ids(count: int, prefix: str = "t") -> list[str]:
    return [f"{prefix}-{number:03d}" for number in range(count)]


def _store(visible: int, hidden: int = 0, *, identity_link: bool = False) -> Any:
    """`visible` in-scope tickets, then `hidden` out-of-scope tickets.

    Every ticket is linked to team ``a``, so a reverse traversal from ``a``
    walks the hidden rows too; scope hides them from the reader.
    """
    ontology = _ontology(identity_link=identity_link)
    store = ObjectStore(ontology.registry)
    store.insert("Team", {"id": "a"}, SRC)
    store.insert("Team", {"id": "empty"}, SRC)
    for obj_id in _ids(visible):
        _insert_ticket(store, obj_id)
    for obj_id in _ids(hidden, "z-hidden"):
        _insert_ticket(store, obj_id, hidden=True)
    return ontology, store


def _args(tool: str, **extra: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "query_objects": {"obj_type": "Ticket"},
        "traverse_links": {
            "obj_type": "Team", "obj_id": "a", "link_api_name": "inTeam", "reverse": True,
        },
    }[tool].copy()
    base.update(extra)
    return base


def _row_ids(result: dict[str, Any]) -> list[str]:
    return [row["payload"]["id"] for row in result["result"]]


def _walk(server: Any, tool: str, limit: int, **extra: Any) -> list[list[str]]:
    """Chain `next_cursor` into `after` until `has_more` is false."""
    pages = []
    result = _call(server, tool, _args(tool, limit=limit, **extra))
    while True:
        pages.append(_row_ids(result))
        assert (result["next_cursor"] is not None) is result["has_more"]
        if not result["has_more"]:
            return pages
        assert len(pages) < 20, "cursor walk did not converge"
        result = _call(server, tool, _args(tool, limit=limit, after=result["next_cursor"], **extra))


# -- AC1: traverse_links pages like query_objects --------------------------------


def test_ac1a_traversal_pages_100_100_50_over_the_known_visible_set() -> None:
    ontology, store = _store(250)
    server, _ = _server(ontology, store)

    pages = _walk(server, "traverse_links", 100)

    assert [len(page) for page in pages] == [100, 100, 50]
    union = [obj_id for page in pages for obj_id in page]
    assert len(union) == len(set(union))
    assert set(union) == set(_ids(250))


def test_ac1b_traversal_limit_above_the_cap_is_invalid_limit() -> None:
    ontology, store = _store(3)
    server, _ = _server(ontology, store)

    result = _call(server, "traverse_links", _args("traverse_links", limit=MAX_LIMIT + 1))

    assert set(result) == {"error"}
    assert result["error"]["code"] == "INVALID_LIMIT"


def test_ac1c_traversal_after_without_limit_is_the_engine_refusal() -> None:
    ontology, store = _store(3)
    server, _ = _server(ontology, store)

    result = _call(server, "traverse_links", _args("traverse_links", after="t-000"))

    assert set(result) == {"error"}
    assert result["error"]["code"] == "AFTER_WITHOUT_LIMIT"
    assert result["error"]["message"] == (
        "traverse: after= was given without limit= -- the "
        "unpaginated path has no page to resume"
    )


# -- AC2: traverse_links defaults to one DEFAULT_LIMIT page -----------------------


def test_ac2_traversal_without_limit_returns_one_default_page() -> None:
    ontology, store = _store(250)
    server, _ = _server(ontology, store)

    result = _call(server, "traverse_links", _args("traverse_links"))

    assert DEFAULT_LIMIT == 100
    assert len(result["result"]) == 100
    assert result["has_more"] is True
    assert isinstance(result["next_cursor"], str) and result["next_cursor"]


# -- AC3: has_more is exact and next_cursor is tight ------------------------------


@pytest.mark.parametrize("tool", LIST_TOOLS)
def test_ac3_exactly_full_page_has_no_more_and_no_cursor(tool: str) -> None:
    ontology, store = _store(100)
    server, _ = _server(ontology, store)

    result = _call(server, tool, _args(tool, limit=100))

    assert len(result["result"]) == 100
    assert result["has_more"] is False
    assert result["next_cursor"] is None


@pytest.mark.parametrize("tool", LIST_TOOLS)
def test_ac3_one_more_visible_row_sets_has_more(tool: str) -> None:
    ontology, store = _store(101)
    server, _ = _server(ontology, store)

    result = _call(server, tool, _args(tool, limit=100))

    assert len(result["result"]) == 100
    assert result["has_more"] is True
    assert isinstance(result["next_cursor"], str) and result["next_cursor"]
    rest = _call(server, tool, _args(tool, limit=100, after=result["next_cursor"]))
    assert _row_ids(rest) == ["t-100"]
    assert rest["has_more"] is False
    assert rest["next_cursor"] is None


# -- AC4: hidden rows never count toward has_more ---------------------------------


@pytest.mark.parametrize("tool", LIST_TOOLS)
def test_ac4_rows_outside_scope_do_not_set_has_more(tool: str) -> None:
    ontology, store = _store(100, hidden=50)
    server, _ = _server(ontology, store)

    result = _call(server, tool, _args(tool, limit=100))

    assert _row_ids(result) == _ids(100)
    assert result["has_more"] is False
    assert result["next_cursor"] is None


# -- AC5: include_total is the visible count, not min-N gated ----------------------


def _min_n_five_store() -> Any:
    ontology, store = _store(3, hidden=2)
    ontology.definition.policy.min_n = 5
    server, client = _server(ontology, store)
    # min-N = 5 is live: a released count over 3 rows refuses.
    released = _call(server, "aggregate_objects", {"obj_type": "Ticket", "func": "count"})
    assert released["error"]["code"] == "MIN_N_VIOLATION"
    return server, client


def test_ac5_query_total_equals_count_objects_under_min_n() -> None:
    server, _ = _min_n_five_store()

    result = _call(server, "query_objects", _args("query_objects", include_total=True))
    count = _call(server, "count_objects", {"obj_type": "Ticket"})

    assert type(result["total"]) is int
    assert result["total"] == 3 == count["result"]
    assert list(result) == ["result", "next_cursor", "has_more", "total", "scope_limited"]


def test_ac5_query_total_follows_where_like_count_objects() -> None:
    server, _ = _min_n_five_store()
    where = {"title": {"in": ["t-000", "t-002"]}}

    result = _call(server, "query_objects", _args("query_objects", where=where, include_total=True))
    count = _call(server, "count_objects", {"obj_type": "Ticket", "where": where})

    assert result["total"] == 2 == count["result"]


def test_ac5_traversal_total_equals_the_paged_union_under_min_n() -> None:
    server, _ = _min_n_five_store()

    first = _call(server, "traverse_links", _args("traverse_links", limit=2, include_total=True))
    pages = _walk(server, "traverse_links", 2)

    union = [obj_id for page in pages for obj_id in page]
    assert first["total"] == 3 == len(union)
    assert list(first) == ["result", "next_cursor", "has_more", "total", "scope_limited"]


@pytest.mark.parametrize("tool", LIST_TOOLS)
@pytest.mark.parametrize("extra", [{}, {"include_total": False}])
def test_ac5_total_key_is_absent_unless_asked(tool: str, extra: dict[str, Any]) -> None:
    server, _ = _min_n_five_store()

    result = _call(server, tool, _args(tool, **extra))

    assert list(result) == ["result", "next_cursor", "has_more", "scope_limited"]


@pytest.mark.parametrize("tool", LIST_TOOLS)
@pytest.mark.parametrize("bad", [1, "true", None])
def test_include_total_must_be_a_json_boolean(tool: str, bad: Any) -> None:
    ontology, store = _store(1)
    server, _ = _server(ontology, store)

    result = _call(server, tool, _args(tool, include_total=bad))

    assert set(result) == {"error"}
    assert result["error"]["code"] == "INVALID_PARAMS"


@pytest.mark.parametrize("tool", LIST_TOOLS)
def test_include_total_schema_defaults_to_false(tool: str) -> None:
    ontology, store = _store(0)
    server, _ = _server(ontology, store)
    tools = {t.name: t for t in asyncio.run(server.list_tools())}

    schema = tools[tool].input_schema["properties"]["include_total"]

    assert schema["type"] == "boolean"
    assert schema["default"] is False


# -- AC6: count-only mode ----------------------------------------------------------


@pytest.mark.parametrize("tool", LIST_TOOLS)
def test_ac6a_count_only_returns_exactly_the_total(tool: str) -> None:
    ontology, store = _store(218, hidden=7)
    server, _ = _server(ontology, store)

    result = _call(server, tool, _args(tool, limit=0, include_total=True))

    assert result == {
        "result": [], "next_cursor": None, "has_more": True, "total": 218, "scope_limited": True,
    }
    assert list(result) == ["result", "next_cursor", "has_more", "total", "scope_limited"]


def test_ac6a_query_count_only_ignores_order_by() -> None:
    ontology, store = _store(5)
    server, _ = _server(ontology, store)

    result = _call(server, "query_objects", _args(
        "query_objects", limit=0, include_total=True, order_by=["title", "desc"],
    ))

    assert result["total"] == 5


@pytest.mark.parametrize("tool", LIST_TOOLS)
def test_ac6b_count_only_on_zero_rows(tool: str) -> None:
    ontology, store = _store(0, hidden=3)
    server, _ = _server(ontology, store)
    arguments = _args(tool, limit=0, include_total=True)
    if tool == "traverse_links":
        arguments["obj_id"] = "empty"

    result = _call(server, tool, arguments)

    assert result == {
        "result": [], "next_cursor": None, "has_more": False, "total": 0, "scope_limited": True,
    }


@pytest.mark.parametrize("tool", LIST_TOOLS)
@pytest.mark.parametrize("extra", [{}, {"include_total": False}])
def test_ac6c_limit_zero_without_include_total_is_invalid_limit(
    tool: str, extra: dict[str, Any],
) -> None:
    ontology, store = _store(3)
    server, _ = _server(ontology, store)

    result = _call(server, tool, _args(tool, limit=0, **extra))

    assert set(result) == {"error"}
    assert result["error"]["code"] == "INVALID_LIMIT"


@pytest.mark.parametrize("tool", LIST_TOOLS)
def test_count_only_takes_no_after(tool: str) -> None:
    ontology, store = _store(3)
    server, _ = _server(ontology, store)

    result = _call(server, tool, _args(tool, limit=0, include_total=True, after="t-000"))

    assert set(result) == {"error"}
    assert result["error"]["code"] == "INVALID_LIMIT"
    assert "count-only" in result["error"]["message"]
    assert "after" in result["error"]["message"]


@pytest.mark.parametrize("arguments", [
    pytest.param({"limit": 0, "include_total": True}, id="count-only"),
    pytest.param({"limit": 0, "include_total": True, "after": "t-000"}, id="count-only-after"),
    pytest.param({"limit": MAX_LIMIT + 1}, id="over-cap"),
    pytest.param({"limit": 0}, id="zero"),
])
def test_identity_revealing_link_is_denied_before_any_paging_rule(
    arguments: dict[str, Any],
) -> None:
    ontology, store = _store(3, identity_link=True)
    server, _ = _server(ontology, store)

    result = _call(server, "traverse_links", _args("traverse_links", **arguments))

    assert set(result) == {"error"}
    assert result["error"]["code"] == "VISIBILITY_DENIED"


@pytest.mark.parametrize("arguments", [
    pytest.param({"limit": 0, "include_total": True}, id="count-only"),
    pytest.param({"limit": MAX_LIMIT + 1}, id="over-cap"),
])
def test_unknown_link_is_unknown_name_before_any_paging_rule(arguments: dict[str, Any]) -> None:
    ontology, store = _store(3)
    server, _ = _server(ontology, store)

    result = _call(server, "traverse_links", _args(
        "traverse_links", link_api_name="Nope", **arguments,
    ))

    assert set(result) == {"error"}
    assert result["error"]["code"] == "UNKNOWN_NAME"


# -- AC7: marks and refusals are unchanged ------------------------------------------


@pytest.mark.parametrize("tool", LIST_TOOLS)
@pytest.mark.parametrize(("arguments", "code"), [
    pytest.param({"obj_type": "Tickte"}, "UNKNOWN_OBJECT_TYPE", id="unknown-type"),
    pytest.param({"limit": MAX_LIMIT + 1}, "INVALID_LIMIT", id="over-cap"),
    pytest.param({"limit": 0}, "INVALID_LIMIT", id="zero"),
    pytest.param({"limit": -1}, "INVALID_LIMIT", id="negative"),
    pytest.param({"limit": 0, "include_total": True, "after": "t-000"}, "INVALID_LIMIT",
                 id="count-only-after"),
    pytest.param({"after": "t-000"}, "AFTER_WITHOUT_LIMIT", id="after-without-limit"),
])
def test_ac7b_refusals_are_exactly_the_error_envelope(
    tool: str, arguments: dict[str, Any], code: str,
) -> None:
    ontology, store = _store(3)
    server, _ = _server(ontology, store)

    result = _call(server, tool, _args(tool, **arguments))

    assert set(result) == {"error"}
    assert set(result["error"]) == {"type", "message", "code", "kind"}
    assert result["error"]["code"] == code


@pytest.mark.parametrize("tool", LIST_TOOLS)
@pytest.mark.parametrize("arguments", [
    pytest.param({"limit": MAX_LIMIT + 1}, id="over-cap"),
    pytest.param({"after": "cursor"}, id="after-without-limit"),
    pytest.param({"limit": 0}, id="zero-without-include-total"),
    pytest.param({"limit": 0, "include_total": True, "after": "cursor"}, id="count-only-after"),
])
def test_ac7c_undeclared_type_precedes_every_paging_rule(
    tool: str, arguments: dict[str, Any],
) -> None:
    ontology, store = _store(3)
    server, _ = _server(ontology, store)

    result = _call(server, tool, _args(tool, obj_type="Tickte", **arguments))

    assert result == GOLDEN_UNKNOWN_TYPE


def test_traversal_paging_schema_matches_query_objects() -> None:
    ontology, store = _store(0)
    server, _ = _server(ontology, store)
    tools = {t.name: t for t in asyncio.run(server.list_tools())}
    query = tools["query_objects"].input_schema["properties"]
    traverse = tools["traverse_links"].input_schema["properties"]

    for name in ("limit", "after", "include_total"):
        assert traverse[name] == query[name], name
    description = tools["traverse_links"].description
    assert description is not None
    assert "no pagination parameters" not in description
    for phrase in ("`has_more`", "`next_cursor`", "`include_total`", "count-only"):
        assert phrase in description
        assert phrase in (tools["query_objects"].description or "")
