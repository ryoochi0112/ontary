"""In-process smoke tests for `ontary.mcp_server` (spec AC8, §7, §8 "MCP
server tools" row): every tool exercised via `MCPServer.call_tool` against the
same toy "library" ontology `test_client.py` uses, with no network/subprocess
involved -- `MCPServer.call_tool` invokes the tool function directly in this
process.

The toy ontology is class-authored (`ontary.authoring.Ontology`, spec
`typed-actions.md` §6): `build_mcp_server(ontology, store, consumer)` lost
its `register_handlers` callback parameter (AC7 clean break), so a server's
action/function handlers must arrive pre-bound on the `Ontology` itself --
class authoring is what makes that auto-binding happen with zero manual
wiring (see `ontary.client`'s `OntologyRuntime` docstring).

Covers:
- introspection tools (`list_object_types`/`list_link_types`/
  `list_action_types`/`list_functions`) match the declared registry defs.
- `get_object`/`query_objects`/`traverse_links` return guarded data for the
  server's bound consumer (redaction applies).
- an out-of-scope `get_object` returns a structured `VISIBILITY_DENIED`
  error, never partial data.
- `execute_action` success and a role-permission denial, both as structured
  payloads (denial as a structured error, never a raised exception reaching
  the MCP caller).
- `call_function` works through the guarded read layer.
- unknown object/action/function names -> structured errors, not raised
  exceptions.
- no `ingest` tool is exposed on the server (spec §5 excludes it).
"""

from __future__ import annotations

import asyncio
import inspect
import json
import re
import sys
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, Protocol

import pytest
from conftest import _mcp_uninstalled, raises_code
from mcp.server.mcpserver import MCPServer

from ontary.actions import ActionContext, ActionError
from ontary.authoring import (
    ActionParams,
    FunctionParams,
    Ontology,
    OntologyObject,
    prop,
    scope_ref,
    target,
)
from ontary.client import OntologyClient
from ontary.errors import (
    AuthorityError,
    ConflictError,
    PermissionDenied,
    VisibilityError,
)
from ontary.functions import BoundQuery
from ontary.mcp_server import (
    DEFAULT_LIMIT,
    MAX_LIMIT,
    MCP_EXTRA_HINT,
    MCP_INCOMPATIBLE_HINT,
    _load_mcp_server,
    build_mcp_server,
    main,
)
from ontary.meta import Cardinality, Sensitivity
from ontary.model import Event
from ontary.scope import DirectProperty, SelfScope, ViaLink
from ontary.security import Consumer
from ontary.store import (
    ObjectStore,
    Source,
)

LEVELS = ["shelf", "library"]
SRC = Source(source_system="test")


def _base_library_ontology(
    *, min_n: int = 1, include_reverse_link: bool = False
) -> tuple[Ontology, type[OntologyObject]]:
    """Declares the toy "library" ontology (Library/Shelf/Book, `inLibrary`/
    `onShelf` links, `RelocateBook` action, `countBooksOnShelf` function) --
    same shapes/behaviors as the pre-M4b hand-registered descriptors, just
    class-authored so `build_mcp_server`/`OntologyClient` auto-bind the
    handlers with zero manual wiring. Deliberately NOT `.validate()`-d here
    (callers may still add e.g. extra functions before freezing) -- returns
    the still-mutable `Ontology` plus its `Book` class (needed by callers
    that declare additional actions against it, e.g. the relabel fixture).
    """
    ontology = Ontology(name="library", scope_levels=LEVELS, min_n=min_n)

    @ontology.object(layer="L0", scope=[SelfScope(level="library")])
    class Library(OntologyObject):
        id: str = prop(primary_key=True)

    @ontology.object(
        layer="L0",
        scope=[
            SelfScope(level="shelf"),
            ViaLink(link_api_name="inLibrary", direction="from", parent_type="Library"),
        ],
    )
    class Shelf(OntologyObject):
        id: str = prop(primary_key=True)

    @ontology.object(
        layer="L0",
        scope=[
            DirectProperty(level="shelf", property_name="shelf_id"),
            ViaLink(link_api_name="onShelf", direction="from", parent_type="Shelf"),
        ],
    )
    class Book(OntologyObject):
        id: str = prop(primary_key=True)
        shelf_id: str | None = prop(default=None, scope_level="shelf")
        title: str | None = None
        # Declared as of Track F4. Eight tests below aggregate `rating`, and
        # it used to reach `aggregate` as an UNDECLARED payload key -- which
        # the store allows, and which `_aggregate` then reduced and released
        # with its declared-type gate skipped. Those tests are about the MCP
        # layer (every func, `where` operators, `group_by`, and a structured
        # `MIN_N_VIOLATION`), not about aggregating an undeclared key; the
        # declaration is what lets each keep testing its own subject.
        rating: float | None = None
        internal_note: str | None = prop(
            default=None, sensitivity=Sensitivity(ai_usable=False, human_visible=True)
        )

    ontology.link(
        "inLibrary", Shelf, Library, Cardinality.MANY_TO_ONE, description="Shelf -> its library"
    )
    onShelf = ontology.link(
        "onShelf",
        Book,
        Shelf,
        Cardinality.MANY_TO_ONE,
        description="Book -> its shelf",
        # Declared ontology-owned (declared-contracts §3 AC1): the
        # RelocateBook handler creates this link from inside
        # ActionExecutor.execute()'s write-capture context.
        owned=True,
    )
    if include_reverse_link:
        ontology.link(
            "relatedShelf",
            Book,
            Shelf,
            Cardinality.MANY_TO_MANY,
            description="Book -> related shelves",
        )

    class _RelocateBookParams(ActionParams):
        book_id: str = target(Book)
        shelf_id: str = scope_ref(Shelf)

    @ontology.action(
        _RelocateBookParams,
        target=Book,
        roles=["Librarian"],
        display_name="Relocate Book",
        description="Move a book to a different shelf",
        api_name="RelocateBook",
    )
    def _relocate(ctx: ActionContext, params: _RelocateBookParams) -> dict[str, str]:
        book = ctx.get(Book, params.book_id)
        if book is None:
            raise ActionError(f"book {params.book_id!r} does not exist", code="PRECONDITION_FAILED")
        ctx.link(onShelf, book, params.shelf_id)
        return {"book_id": params.book_id}

    class CountBooksOnShelfParams(FunctionParams):
        shelf_id: str

    @ontology.function(
        CountBooksOnShelfParams,
        description="Count the (visible) books on a shelf",
        input_description="shelf_id",
        output_description="int count",
        api_name="countBooksOnShelf",
    )
    def _count_books_on_shelf(
        query: BoundQuery, params: CountBooksOnShelfParams
    ) -> int:
        return len(
            query.list(
                "Book", where={"shelf_id": params.shelf_id}, limit=None
            )
        )

    return ontology, Book


def _librarian(scope_id: str = "shelf-1") -> Consumer:
    return Consumer(
        actor_id="lib-1",
        role="Librarian",
        scope_level="shelf",
        scope_id=scope_id,
        kind="human",
    )


def _library_librarian(scope_id: str = "lib-1") -> Consumer:
    return Consumer(
        actor_id="head-lib-1",
        role="Librarian",
        scope_level="library",
        scope_id=scope_id,
        kind="human",
    )


def _build_server(
    consumer: Consumer, *, include_reverse_link: bool = False
) -> tuple[MCPServer, ObjectStore]:
    ontology, _Book = _base_library_ontology(include_reverse_link=include_reverse_link)
    ontology.validate()
    store = ObjectStore(ontology.registry)
    store.insert("Library", {"id": "lib-1"}, SRC)
    store.insert("Shelf", {"id": "shelf-1", "library_id": "lib-1"}, SRC)
    store.create_link("inLibrary", "shelf-1", "lib-1")
    store.insert("Shelf", {"id": "shelf-2", "library_id": "lib-1"}, SRC)
    store.create_link("inLibrary", "shelf-2", "lib-1")
    store.insert(
        "Book",
        {
            "id": "book-1",
            "shelf_id": "shelf-1",
            "title": "Foundry",
            "internal_note": "shhh",
        },
        SRC,
    )
    if include_reverse_link:
        store.insert("Book", {"id": "book-c", "shelf_id": "shelf-2"}, SRC)
        store.create_link("relatedShelf", "book-1", "shelf-1")
        store.create_link("relatedShelf", "book-c", "shelf-1")
        store.create_link("relatedShelf", "book-1", "shelf-2")

    server = build_mcp_server(ontology, store, consumer)
    return server, store


def _call(server: MCPServer, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """Call `name` in-process (no network/subprocess) and decode its payload."""
    result = asyncio.run(server.call_tool(name, arguments))
    if result.structured_content is not None:
        return result.structured_content
    payload: dict[str, Any] = json.loads(result.content[0].text)
    return payload


# -- introspection --------------------------------------------------------


def test_every_registered_tool_is_a_coroutine_function() -> None:
    server, _store = _build_server(_librarian())
    tool_names = [tool.name for tool in asyncio.run(server.list_tools())]
    assert tool_names, "server registered no tools"
    for name in tool_names:
        tool = server._tool_manager.get_tool(name)
        assert inspect.iscoroutinefunction(tool.fn), (
            f"registered tool {name!r} is not a coroutine function"
        )


def test_list_object_types_matches_registry() -> None:
    server, _ = _build_server(_librarian())
    payload = _call(server, "list_object_types", {})
    names = {d["api_name"] for d in payload["object_types"]}
    assert names == {"Library", "Shelf", "Book"}
    book = next(d for d in payload["object_types"] if d["api_name"] == "Book")
    assert {p["name"] for p in book["properties"]} == {
        "id",
        "shelf_id",
        "title",
        "rating",
        "internal_note",
    }


def test_build_mcp_server_accepts_and_threads_provider_maps() -> None:
    class Reader(Protocol):
        def read(self) -> str: ...

    class ReaderProvider:
        def read(self) -> str:
            return "mcp"

    ontology, _Book = _base_library_ontology()
    capability = ontology.capability(Reader)
    provider = ReaderProvider()

    class MetadataParams(ActionParams):
        book_id: str = target(_Book)

    @ontology.action(
        MetadataParams,
        target=_Book,
        roles=["Librarian"],
        capabilities=[capability],
        api_name="metadataOnly",
    )
    def metadata_only(
        _ctx: ActionContext, params: MetadataParams
    ) -> dict[str, str]:
        # Deliberately does not access the declaration: metadata describes
        # allowed dependencies, not observed runtime use.
        return {"book_id": params.book_id}

    @ontology.function(api_name="providerBindings", capabilities=[capability])
    def provider_bindings(query: BoundQuery) -> dict[str, bool]:
        return {"capability": query._capability_providers[capability] is provider}

    ontology.validate()
    server = build_mcp_server(
        ontology,
        ObjectStore(ontology.registry),
        _librarian(),
        capabilities={capability: provider},
    )

    assert _call(
        server,
        "call_function",
        {"api_name": "providerBindings", "params": {}},
    ) == {"result": {"capability": True}}
    action_payload = _call(server, "list_action_types", {})["action_types"]
    metadata_action = next(
        action for action in action_payload if action["api_name"] == "metadataOnly"
    )
    assert metadata_action["capabilities"] == ["Reader"]
    function_payload = _call(server, "list_functions", {})["functions"]
    provider_function = next(
        fn for fn in function_payload if fn["api_name"] == "providerBindings"
    )
    assert provider_function["capabilities"] == ["Reader"]


def test_list_link_types_matches_registry() -> None:
    server, _ = _build_server(_librarian())
    payload = _call(server, "list_link_types", {})
    names = {d["api_name"] for d in payload["link_types"]}
    assert names == {"inLibrary", "onShelf"}


def test_list_action_types_matches_registry() -> None:
    server, _ = _build_server(_librarian())
    payload = _call(server, "list_action_types", {})
    names = {d["api_name"] for d in payload["action_types"]}
    assert names == {"RelocateBook"}
    action = payload["action_types"][0]
    assert {p["name"] for p in action["parameters"]} == {"book_id", "shelf_id"}
    assert action["emits"] == []


def test_list_action_types_lists_emitted_event_api_names() -> None:
    ontology, Book = _base_library_ontology()

    @ontology.event(api_name="bookRelabeled")
    class BookRelabeled(Event):
        title: str

    @ontology.event(api_name="bookReviewed")
    class BookReviewed(Event):
        rating: float

    class RelabelBook(ActionParams):
        book_id: str = target(Book)

    @ontology.action(
        RelabelBook, target=Book, roles=["Librarian"],
        emits=[BookRelabeled, BookReviewed],
    )
    def relabel(_ctx: ActionContext, _params: RelabelBook) -> dict[str, Any]:
        return {}

    ontology.validate()
    server = build_mcp_server(ontology, ObjectStore(ontology.registry), _librarian())
    actions = {
        action["api_name"]: action
        for action in _call(server, "list_action_types", {})["action_types"]
    }
    assert actions["RelabelBook"]["emits"] == ["bookRelabeled", "bookReviewed"]


def test_list_functions_matches_registry() -> None:
    server, _ = _build_server(_librarian())
    payload = _call(server, "list_functions", {})
    names = {d["api_name"] for d in payload["functions"]}
    assert names == {"countBooksOnShelf"}


def test_typed_function_parameters_are_published_and_validated() -> None:
    ontology, _Book = _base_library_ontology()

    class TicketStatsParams(FunctionParams):
        queue_id: str

    @ontology.function(
        TicketStatsParams,
        description="Mean ticket age (hours) for a queue.",
        input_description="A queue_id.",
        output_description="A mean age_hours value.",
        api_name="ticketStats",
    )
    def ticket_stats(_query: BoundQuery, params: TicketStatsParams) -> str:
        return params.queue_id

    @ontology.function(api_name="noInputs")
    def no_inputs(_query: BoundQuery) -> str:
        return "ok"

    ontology.validate()
    server = build_mcp_server(ontology, ObjectStore(ontology.registry), _librarian())

    functions = _call(server, "list_functions", {})["functions"]
    ticket_stats_payload = next(fn for fn in functions if fn["api_name"] == "ticketStats")
    assert ticket_stats_payload == {
        "api_name": "ticketStats",
        "description": "Mean ticket age (hours) for a queue.",
        "input_description": "A queue_id.",
        "output_description": "A mean age_hours value.",
        "capabilities": [],
        "parameters": [
            {
                "name": "queue_id",
                "type": "str",
                "description": None,
                "choices": None,
                "fields": None,
                "required": True,
                "refers_to": None,
            }
        ],
    }
    assert next(fn for fn in functions if fn["api_name"] == "noInputs")["parameters"] == []
    assert next(
        fn for fn in functions if fn["api_name"] == "countBooksOnShelf"
    )["parameters"] == [
        {
            "name": "shelf_id",
            "type": "str",
            "description": None,
            "choices": None,
            "fields": None,
            "required": True,
            "refers_to": None,
        }
    ]

    valid = _call(
        server,
        "call_function",
        {"api_name": "ticketStats", "params": {"queue_id": "q1"}},
    )
    assert valid == {"result": "q1"}
    invalid = _call(
        server,
        "call_function",
        {"api_name": "ticketStats", "params": {}},
    )
    assert invalid["error"]["code"] == "INVALID_PARAMS"
    assert "queue_id" in invalid["error"]["message"]


# -- guarded reads ----------------------------------------------------------


# The exact wire shape of a `lineage` object -- pinned so a future field
# (e.g. a store-internal row identity a later change forgets to keep
# out-of-band, spec pagination-hardening §5) can't silently leak onto the
# wire without a test noticing.
_LINEAGE_WIRE_KEYS = {
    "object_type",
    "object_id",
    "valid_from",
    "valid_to",
    "source_system",
    "source_id",
    "extracted_at",
}


def test_get_object_returns_guarded_data() -> None:
    server, _ = _build_server(_librarian())
    payload = _call(server, "get_object", {"obj_type": "Book", "obj_id": "book-1"})
    result = payload["result"]
    assert result["payload"]["title"] == "Foundry"
    assert result["payload"]["internal_note"] == "shhh"  # human consumer sees it
    assert result["lineage"]["object_type"] == "Book"
    assert result["lineage"]["object_id"] == "book-1"
    assert result["lineage"]["source_system"] == "test"
    assert set(result["lineage"].keys()) == _LINEAGE_WIRE_KEYS


def test_query_objects_returns_visible_rows() -> None:
    server, _ = _build_server(_librarian())
    payload = _call(server, "query_objects", {"obj_type": "Book"})
    assert [r["payload"]["id"] for r in payload["result"]] == ["book-1"]
    assert [r["lineage"]["object_type"] for r in payload["result"]] == ["Book"]
    assert all(set(r["lineage"].keys()) == _LINEAGE_WIRE_KEYS for r in payload["result"])


def test_query_objects_unknown_where_key_is_structured_error() -> None:
    server, _ = _build_server(_librarian())
    payload = _call(
        server,
        "query_objects",
        {"obj_type": "Book", "where": {"titlle": "Foundry"}},
    )
    assert payload == {
        "error": {
            "type": "ValidationFailed",
            "message": "Book: where= names unknown field(s) ['titlle']",
            "code": "UNKNOWN_FIELD",
            "kind": "validation",
        }
    }


def test_query_objects_accepts_the_same_operator_json_as_python_reads() -> None:
    server, store = _build_server(_librarian())
    store.insert(
        "Book",
        {
            "id": "book-2",
            "shelf_id": "shelf-1",
            "title": "Foundry companion",
        },
        SRC,
    )
    where = {"title": {"contains": "Found"}}

    payload = _call(server, "query_objects", {"obj_type": "Book", "where": where})

    assert [row["payload"]["id"] for row in payload["result"]] == [
        "book-1",
        "book-2",
    ]


def test_query_objects_accepts_json_order_by_pair() -> None:
    server, store = _build_server(_librarian())
    store.insert(
        "Book",
        {
            "id": "book-2",
            "shelf_id": "shelf-1",
            "title": "Zen companion",
        },
        SRC,
    )

    payload = _call(
        server,
        "query_objects",
        {"obj_type": "Book", "order_by": ["title", "desc"]},
    )

    assert [row["payload"]["id"] for row in payload["result"]] == [
        "book-2",
        "book-1",
    ]


def test_query_objects_operator_type_refusal_is_structured() -> None:
    server, _ = _build_server(_librarian())

    payload = _call(
        server,
        "query_objects",
        {"obj_type": "Book", "where": {"title": {"gt": "Foundry"}}},
    )

    assert payload["error"]["code"] == "OPERATOR_TYPE_MISMATCH"
    assert payload["error"]["kind"] == "validation"


def test_count_objects_counts_only_visible_rows_with_operator_where() -> None:
    server, store = _build_server(_librarian())
    store.insert(
        "Book",
        {"id": "book-2", "shelf_id": "shelf-1", "title": "Foundry companion"},
        SRC,
    )
    store.insert(
        "Book",
        {"id": "book-3", "shelf_id": "shelf-2", "title": "Foundry hidden"},
        SRC,
    )
    store.insert(
        "Book",
        {"id": "book-4", "shelf_id": "shelf-1", "title": "Unmatched"},
        SRC,
    )

    payload = _call(
        server,
        "count_objects",
        {"obj_type": "Book", "where": {"title": {"contains": "Foundry"}}},
    )

    assert payload == {"result": 2, "scope_limited": True}


def test_count_objects_returns_zero_when_consumer_is_scoped_away() -> None:
    server, _store = _build_server(_librarian("shelf-2"))

    assert _call(server, "count_objects", {"obj_type": "Book"}) == {
        "result": 0, "scope_limited": True,
    }


def test_count_objects_operator_refusal_is_structured() -> None:
    server, _store = _build_server(_librarian())

    payload = _call(
        server,
        "count_objects",
        {"obj_type": "Book", "where": {"title": {"between": ["A", "Z"]}}},
    )

    assert payload["error"]["code"] == "UNKNOWN_OPERATOR"
    assert payload["error"]["kind"] == "validation"


@pytest.mark.parametrize(
    ("func", "expected"),
    [
        ("mean", 3.0),
        ("count", 2),
        ("sum", 6.0),
        ("min", 2.0),
        ("max", 4.0),
    ],
)
def test_aggregate_objects_accepts_every_function_and_operator_where(
    func: str, expected: int | float
) -> None:
    server, store = _build_server(_librarian())
    store.insert(
        "Book",
        {
            "id": "book-2",
            "shelf_id": "shelf-1",
            "title": "rated low",
            "rating": 2.0,
        },
        SRC,
    )
    store.insert(
        "Book",
        {
            "id": "book-3",
            "shelf_id": "shelf-1",
            "title": "rated high",
            "rating": 4.0,
        },
        SRC,
    )

    payload = _call(
        server,
        "aggregate_objects",
        {
            "obj_type": "Book",
            "value_field": "rating",
            "where": {"title": {"contains": "rated"}},
            "func": func,
        },
    )

    assert payload == {"result": pytest.approx(expected)}


def test_aggregate_objects_accepts_group_by_and_defaults_to_mean() -> None:
    server, store = _build_server(_library_librarian())
    store.insert(
        "Book",
        {
            "id": "book-2",
            "shelf_id": "shelf-1",
            "title": "rated low",
            "rating": 2.0,
        },
        SRC,
    )
    store.insert(
        "Book",
        {
            "id": "book-3",
            "shelf_id": "shelf-2",
            "title": "rated high",
            "rating": 4.0,
        },
        SRC,
    )

    payload = _call(
        server,
        "aggregate_objects",
        {
            "obj_type": "Book",
            "value_field": "rating",
            "group_by": "shelf_id",
            "where": {"title": {"contains": "rated"}},
        },
    )

    assert payload == {"result": {"shelf-1": 2.0, "shelf-2": 4.0}}


@pytest.mark.parametrize("func", [None, "median"])
def test_aggregate_objects_invalid_function_is_structured(func: object) -> None:
    server, _store = _build_server(_librarian())

    payload = _call(
        server,
        "aggregate_objects",
        {"obj_type": "Book", "value_field": "rating", "func": func},
    )

    assert payload["error"]["code"] == "INVALID_PARAMS"
    assert payload["error"]["kind"] == "validation"


def test_aggregate_objects_count_without_value_field_returns_released_count() -> None:
    """`func="count"` with no `value_field` counts every visible row --
    the min-N-released alternative `count_objects`'s description points
    to (spec `count-min-n-discoverability.md` §2 item 2)."""
    server, store = _build_server(_librarian())
    store.insert("Book", {"id": "book-2", "shelf_id": "shelf-1"}, SRC)

    payload = _call(
        server,
        "aggregate_objects",
        {"obj_type": "Book", "func": "count"},
    )

    assert payload == {"result": 2}


def test_aggregate_objects_count_without_value_field_is_min_n_gated() -> None:
    """Below the dedicated policy's `min_n=3`, `func="count"` with no
    `value_field` still refuses with a structured `MIN_N_VIOLATION` -- it
    is released, not a bypass."""
    server = _build_min_n_server(_librarian())

    payload = _call(
        server,
        "aggregate_objects",
        {"obj_type": "Book", "where": {"shelf_id": "shelf-1"}, "func": "count"},
    )

    assert payload["error"]["code"] == "MIN_N_VIOLATION"
    assert payload["error"]["kind"] == "visibility"
    assert "result" not in payload


def test_aggregate_objects_mean_without_value_field_is_invalid_params() -> None:
    server, _store = _build_server(_librarian())

    payload = _call(
        server,
        "aggregate_objects",
        {"obj_type": "Book", "func": "mean"},
    )

    assert payload["error"]["code"] == "INVALID_PARAMS"
    assert payload["error"]["kind"] == "validation"
    assert "value_field" in payload["error"]["message"]
    assert "mean" in payload["error"]["message"]


def test_aggregate_objects_counts_a_declared_string_field() -> None:
    """`func="count"` over a declared `str` field (`title`) counts rows
    carrying it, with no float coercion (spec §2 item 1)."""
    server, store = _build_server(_librarian())
    store.insert("Book", {"id": "book-2", "shelf_id": "shelf-1"}, SRC)  # no title

    payload = _call(
        server,
        "aggregate_objects",
        {"obj_type": "Book", "value_field": "title", "func": "count"},
    )

    assert payload == {"result": 1}


def _build_many_books_server(count: int) -> tuple[MCPServer, ObjectStore]:
    """Build the shared library fixture with exactly `count` visible books."""
    server, store = _build_server(_librarian())
    for number in range(2, count + 1):
        store.insert(
            "Book",
            {
                "id": f"book-{number}",
                "shelf_id": "shelf-1",
                "title": f"Book {number}",
            },
            SRC,
        )
    return server, store


def test_query_objects_default_cap_paginates_and_pins_100() -> None:
    server, _ = _build_many_books_server(105)

    assert DEFAULT_LIMIT == 100
    first = _call(server, "query_objects", {"obj_type": "Book"})
    assert len(first["result"]) == 100
    assert first["next_cursor"] is not None

    rows = list(first["result"])
    cursor = first["next_cursor"]
    pages = 0
    while cursor is not None:
        pages += 1
        assert pages <= 2, "default-cap cursor walk did not converge"
        page = _call(
            server,
            "query_objects",
            {"obj_type": "Book", "limit": DEFAULT_LIMIT, "after": cursor},
        )
        rows.extend(page["result"])
        cursor = page["next_cursor"]

    assert len(rows) == 105
    assert len({row["payload"]["id"] for row in rows}) == 105


def test_query_objects_hard_max_accepts_1000_and_refuses_1001() -> None:
    server, _ = _build_many_books_server(1001)

    assert MAX_LIMIT == 1000
    accepted = _call(
        server, "query_objects", {"obj_type": "Book", "limit": MAX_LIMIT}
    )
    assert len(accepted["result"]) == 1000
    assert accepted["next_cursor"] is not None

    refused = _call(
        server, "query_objects", {"obj_type": "Book", "limit": MAX_LIMIT + 1}
    )
    assert refused["error"]["code"] == "INVALID_LIMIT"


@pytest.mark.parametrize("bad_limit", [0, -1])
def test_query_objects_non_positive_limit_uses_underlying_validation(
    bad_limit: int,
) -> None:
    server, _ = _build_server(_librarian())

    payload = _call(
        server, "query_objects", {"obj_type": "Book", "limit": bad_limit}
    )
    assert payload["error"]["code"] == "INVALID_LIMIT"
    assert payload["error"]["message"] == (
        f"get_objects limit must be >= 1, got {bad_limit!r}"
    )


def test_query_objects_after_without_limit_uses_underlying_validation() -> None:
    server, _ = _build_server(_librarian())

    payload = _call(
        server, "query_objects", {"obj_type": "Book", "after": "some-cursor"}
    )
    assert payload["error"]["code"] == "AFTER_WITHOUT_LIMIT"
    assert payload["error"]["message"] == (
        "get_objects: after= was given without limit= -- the "
        "unpaginated path has no page to resume"
    )


def test_mcp_read_annotations_and_execute_destructive_annotation() -> None:
    server, _ = _build_server(_librarian())
    tools = {tool.name: tool for tool in asyncio.run(server.list_tools())}
    read_names = {
        "list_object_types",
        "list_link_types",
        "list_action_types",
        "list_functions",
        "list_event_types",
        "list_events",
        "get_declarations",
        "get_object",
        "query_objects",
        "count_objects",
        "aggregate_objects",
        "traverse_links",
        "call_function",
    }

    for name in read_names:
        annotations = tools[name].annotations
        assert annotations is not None
        assert annotations.read_only_hint is True

    execute_annotations = tools["execute_action"].annotations
    assert execute_annotations is not None
    assert execute_annotations.destructive_hint is True


def test_query_objects_description_and_schema_state_where_and_pagination_contract() -> None:
    server, _ = _build_server(_librarian())
    query_tool = next(
        tool for tool in asyncio.run(server.list_tools()) if tool.name == "query_objects"
    )
    description = query_tool.description
    assert description is not None
    for phrase in (
        "typed operator matching on declared payload fields",
        "unknown keys raise `UNKNOWN_FIELD`",
        "`limit` defaults to 100",
        "hard maximum of 1000",
        "`after`",
        "`next_cursor`",
    ):
        assert phrase in description

    where_description = query_tool.input_schema["properties"]["where"]["description"]
    assert "Typed operator matching on declared payload fields" in where_description
    assert "unknown keys raise UNKNOWN_FIELD" in where_description


def test_count_objects_description_points_to_the_released_alternative() -> None:
    """The `count_objects` description an AI reads back from `list_tools`
    must name it as a visible-row count that is not min-N-gated, and point
    to `aggregate_objects(func="count")` as the released alternative (spec
    `count-min-n-discoverability.md` §2 item 4)."""
    server, _ = _build_server(_librarian())
    count_tool = next(
        tool for tool in asyncio.run(server.list_tools()) if tool.name == "count_objects"
    )
    description = count_tool.description
    assert description is not None
    assert "not min-N-gated" in description
    assert "aggregate_objects" in description


def test_aggregate_objects_value_field_schema_mentions_count() -> None:
    server, _ = _build_server(_librarian())
    aggregate_tool = next(
        tool for tool in asyncio.run(server.list_tools()) if tool.name == "aggregate_objects"
    )
    value_field_description = aggregate_tool.input_schema["properties"]["value_field"][
        "description"
    ]
    assert "count" in value_field_description


def test_query_objects_has_no_unbounded_mcp_parameter_combination() -> None:
    server, _ = _build_many_books_server(1001)
    assert MAX_LIMIT == 1000

    combinations = (
        {"obj_type": "Book"},
        {"obj_type": "Book", "limit": None},
        {"obj_type": "Book", "limit": MAX_LIMIT},
        {"obj_type": "Book", "limit": MAX_LIMIT + 1},
    )
    for arguments in combinations:
        payload = _call(server, "query_objects", arguments)
        if "error" in payload:
            assert payload["error"]["code"] == "INVALID_LIMIT"
            continue
        assert len(payload["result"]) <= MAX_LIMIT


def test_traverse_links_follows_declared_link() -> None:
    server, _ = _build_server(_library_librarian())
    payload = _call(
        server,
        "traverse_links",
        {"obj_type": "Shelf", "obj_id": "shelf-1", "link_api_name": "inLibrary"},
    )
    assert [r["payload"]["id"] for r in payload["result"]] == ["lib-1"]
    assert [r["lineage"]["object_type"] for r in payload["result"]] == ["Library"]


def test_traverse_links_reverse_flag_is_symmetric_and_pinned() -> None:
    """The old suite could build the live tool but only asserted that the
    reverse property existed; it could not catch a widened nullable schema.
    Pin the complete public property schema so that framework-boundary typing
    cannot advertise an input the body refuses.
    """
    server, _ = _build_server(_library_librarian(), include_reverse_link=True)
    tool = next(tool for tool in asyncio.run(server.list_tools()) if tool.name == "traverse_links")
    assert tool.input_schema["properties"]["reverse"] == {
        "type": "boolean",
        "default": False,
        "title": "Reverse",
    }

    forward = _call(
        server,
        "traverse_links",
        {"obj_type": "Book", "obj_id": "book-1", "link_api_name": "relatedShelf"},
    )
    reverse = _call(
        server,
        "traverse_links",
        {
            "obj_type": "Shelf",
            "obj_id": "shelf-1",
            "link_api_name": "relatedShelf",
            "reverse": True,
        },
    )
    explicit_forward = _call(
        server,
        "traverse_links",
        {
            "obj_type": "Book",
            "obj_id": "book-1",
            "link_api_name": "relatedShelf",
            "reverse": False,
        },
    )
    assert {row["payload"]["id"] for row in forward["result"]} == {"shelf-1", "shelf-2"}
    assert explicit_forward == forward
    assert {row["payload"]["id"] for row in reverse["result"]} == {"book-1", "book-c"}


def test_get_object_out_of_scope_returns_structured_denial() -> None:
    server, _ = _build_server(_librarian(scope_id="shelf-OTHER"))
    payload = _call(server, "get_object", {"obj_type": "Book", "obj_id": "book-1"})
    assert payload["error"]["type"] == "VisibilityError"
    assert payload["error"]["code"] == "VISIBILITY_DENIED"
    assert payload["error"]["kind"] == "visibility"
    assert "message" in payload["error"]
    assert "result" not in payload  # never partial data alongside an error


# -- actions ----------------------------------------------------------------


def test_execute_action_success() -> None:
    server, _ = _build_server(_library_librarian())
    payload = _call(
        server,
        "execute_action",
        {"api_name": "RelocateBook", "params": {"book_id": "book-1", "shelf_id": "shelf-2"}},
    )
    assert payload == {"result": {"book_id": "book-1"}}


def test_execute_action_permission_denial_is_structured() -> None:
    consumer = Consumer(
        actor_id="volunteer-1",
        role="Volunteer",
        scope_level="shelf",
        scope_id="shelf-1",
        kind="human",
    )
    server, _ = _build_server(consumer)
    payload = _call(
        server,
        "execute_action",
        {"api_name": "RelocateBook", "params": {"book_id": "book-1", "shelf_id": "shelf-2"}},
    )
    assert payload["error"]["type"] == "PermissionDenied"
    assert payload["error"]["code"] == "PERMISSION_DENIED"
    assert payload["error"]["kind"] == "permission"


# -- functions ----------------------------------------------------------------


def test_call_function_reads_through_guarded_query() -> None:
    server, _ = _build_server(_librarian())
    payload = _call(
        server, "call_function", {"api_name": "countBooksOnShelf", "params": {"shelf_id": "shelf-1"}}
    )
    assert payload["result"] == 1


def _avg_rating(query: BoundQuery) -> float:
    return query.aggregate("Book", "rating")


def _avg_rating_by_shelf(query: BoundQuery) -> dict[str, float]:
    return query.aggregate_by("Book", "rating", "shelf_id")


def _blow_up(query: BoundQuery) -> Any:
    raise RuntimeError("secret internals")


def _build_min_n_server(consumer: Consumer) -> MCPServer:
    """A dedicated ontology with `min_n=3` (unlike `_build_server`'s
    `min_n=1`) so an aggregate over fewer than 3 contributors actually
    trips `MIN_N_VIOLATION` -- proving the structured error survives the MCP
    layer (reviewer finding: untestable with the shared fixture's min_n=1).
    """
    ontology, _Book = _base_library_ontology(min_n=3)

    @ontology.function(
        description="Average book rating on a shelf (min-N gated)",
        input_description="shelf_id",
        output_description="float average",
        api_name="avgRating",
    )
    def _avg(query: BoundQuery) -> float:
        return _avg_rating(query)

    @ontology.function(
        description="Average book rating by shelf (min-N gated)",
        input_description="none",
        output_description="mapping of shelf to average",
        api_name="avgRatingByShelf",
    )
    def _avg_by_shelf(query: BoundQuery) -> dict[str, float]:
        return _avg_rating_by_shelf(query)

    @ontology.function(
        description="Always raises, to exercise the InternalError path",
        input_description="none",
        output_description="never returns",
        api_name="blowUp",
    )
    def _blow(query: BoundQuery) -> Any:
        return _blow_up(query)

    ontology.validate()
    store = ObjectStore(ontology.registry)
    store.insert("Library", {"id": "lib-1"}, SRC)
    store.insert("Shelf", {"id": "shelf-1", "library_id": "lib-1"}, SRC)
    store.create_link("inLibrary", "shelf-1", "lib-1")
    store.insert("Book", {"id": "book-1", "shelf_id": "shelf-1", "rating": 4.0}, SRC)
    store.insert("Book", {"id": "book-2", "shelf_id": "shelf-1", "rating": 5.0}, SRC)
    return build_mcp_server(ontology, store, consumer)


def test_call_function_min_n_violation_is_structured() -> None:
    """Only 2 books are on shelf-1 (below the dedicated policy's min_n=3):
    the resulting `MIN_N_VIOLATION` refusal must reach the MCP caller as a structured
    error, never a raised exception or partial aggregate value."""
    server = _build_min_n_server(_librarian())
    payload = _call(
        server, "call_function", {"api_name": "avgRating", "params": {}}
    )
    assert payload["error"]["type"] == "VisibilityError"
    assert payload["error"]["code"] == "MIN_N_VIOLATION"
    assert payload["error"]["kind"] == "visibility"
    assert "2" not in payload["error"]["message"]
    assert "withheld" in payload["error"]["message"]
    assert "result" not in payload


def test_call_function_grouped_min_n_violation_withholds_count() -> None:
    server = _build_min_n_server(_librarian())
    payload = _call(
        server,
        "call_function",
        {"api_name": "avgRatingByShelf", "params": {}},
    )

    assert payload["error"]["type"] == "VisibilityError"
    assert payload["error"]["code"] == "MIN_N_VIOLATION"
    assert payload["error"]["kind"] == "visibility"
    assert "2" not in payload["error"]["message"]
    assert "withheld" in payload["error"]["message"]


def test_call_function_internal_error_is_generic_and_hides_internals() -> None:
    """A handler raising an unexpected exception (anything not in
    `_KNOWN_ERRORS`) must never leak its message to the MCP caller -- the
    payload is exactly the generic `InternalError` envelope, and the
    original message ("secret internals") appears nowhere in it."""
    server = _build_min_n_server(_librarian())
    payload = _call(server, "call_function", {"api_name": "blowUp", "params": {}})
    assert payload == {
        "error": {
            "type": "InternalError",
            "message": "internal server error",
            "code": "INTERNAL_ERROR",
            "kind": "internal",
        }
    }
    assert "secret" not in json.dumps(payload)


# -- governed capability error codes (spec AC16) -----------------------------


def _build_governed_error_server() -> MCPServer:
    class Reader(Protocol):
        def read(self) -> str: ...

    ontology, Book = _base_library_ontology()
    reader = ontology.capability(Reader)

    @ontology.function(api_name="undeclaredCapability")
    def undeclared_capability(query: BoundQuery) -> str:
        return query.capability(reader).read()

    @ontology.function(api_name="missingCapability", capabilities=[reader])
    def missing_capability(query: BoundQuery) -> str:
        return query.capability(reader).read()

    ontology.validate()
    store = ObjectStore(ontology.registry)
    store.insert("Library", {"id": "lib-1"}, SRC)
    store.insert("Shelf", {"id": "shelf-1", "library_id": "lib-1"}, SRC)
    store.create_link("inLibrary", "shelf-1", "lib-1")
    store.insert("Book", {"id": "book-1", "shelf_id": "shelf-1"}, SRC)
    return build_mcp_server(ontology, store, _library_librarian())


def test_mcp_preserves_undeclared_capability_code_and_kind() -> None:
    payload = _call(
        _build_governed_error_server(),
        "call_function",
        {"api_name": "undeclaredCapability", "params": {}},
    )
    assert payload["error"]["type"] == "ValidationFailed"
    assert payload["error"]["code"] == "UNDECLARED_CAPABILITY"
    assert payload["error"]["kind"] == "validation"


def test_mcp_preserves_capability_not_provided_code_and_kind() -> None:
    payload = _call(
        _build_governed_error_server(),
        "call_function",
        {"api_name": "missingCapability", "params": {}},
    )
    assert payload["error"]["type"] == "PreconditionFailed"
    assert payload["error"]["code"] == "CAPABILITY_NOT_PROVIDED"
    assert payload["error"]["kind"] == "precondition"


# -- unknown names ------------------------------------------------------------


def test_get_object_unknown_id_returns_no_data() -> None:
    # An unregistered object type/id is simply absent from the store (the
    # same "not found" the underlying `GuardedQuery.get_object` returns for
    # any bare-store miss, existing engine behavior this task does not
    # change) -- no error, but critically no data either.
    server, _ = _build_server(_librarian())
    payload = _call(server, "get_object", {"obj_type": "Book", "obj_id": "does-not-exist"})
    assert payload == {"result": None}


def test_execute_action_unknown_action_is_structured_error() -> None:
    server, _ = _build_server(_librarian())
    payload = _call(server, "execute_action", {"api_name": "Nope", "params": {}})
    assert payload["error"]["type"] == "ValidationFailed"
    assert payload["error"]["code"] == "UNKNOWN_ACTION"
    assert payload["error"]["kind"] == "validation"


def test_call_function_unknown_function_is_structured_error() -> None:
    server, _ = _build_server(_librarian())
    payload = _call(server, "call_function", {"api_name": "Nope", "params": {}})
    assert payload["error"]["type"] == "PreconditionFailed"
    assert payload["error"]["code"] == "FUNCTION_ERROR"
    assert payload["error"]["kind"] == "precondition"


def test_traverse_links_unknown_link_is_structured_error() -> None:
    server, _ = _build_server(_librarian())
    payload = _call(
        server,
        "traverse_links",
        {"obj_type": "Book", "obj_id": "book-1", "link_api_name": "nope"},
    )
    assert payload["error"]["type"] == "ValidationFailed"
    assert payload["error"]["code"] == "UNKNOWN_NAME"
    assert payload["error"]["kind"] == "validation"


# -- no ingest surface --------------------------------------------------------


def test_no_ingest_tool_exposed() -> None:
    server, _ = _build_server(_librarian())
    tool_names = {t.name for t in asyncio.run(server.list_tools())}
    assert "ingest" not in tool_names
    assert "ingest_links" not in tool_names
    assert tool_names == {
        "list_object_types",
        "list_link_types",
        "list_action_types",
        "list_functions",
        "list_event_types",
        "list_events",
        "get_declarations",
        "get_object",
        "query_objects",
        "count_objects",
        "aggregate_objects",
        "traverse_links",
        "execute_action",
        "call_function",
    }


# -- declarations (spec AC10) -------------------------------------------------


def test_get_declarations_matches_client_declarations() -> None:
    server, store = _build_server(_librarian())
    ontology, _Book = _base_library_ontology()
    ontology.validate()
    client = OntologyClient(ontology, store, _librarian())

    payload = _call(server, "get_declarations", {})
    assert payload["result"] == client.declarations.model_dump()


# -- client/MCP error-code parity (spec AC8) ----------------------------------


def _client_and_server(consumer: Consumer) -> tuple[OntologyClient, MCPServer]:
    """A client and an MCP server bound to the SAME (ontology, store,
    consumer) triple, for asserting the two surfaces raise/report the same
    stable code for the same refusal."""
    server, store = _build_server(consumer)
    ontology, _Book = _base_library_ontology()
    ontology.validate()
    client = OntologyClient(ontology, store, consumer)
    return client, server


def test_parity_visibility_denied_code() -> None:
    consumer = _librarian(scope_id="shelf-OTHER")
    client, server = _client_and_server(consumer)

    with raises_code(VisibilityError, "VISIBILITY_DENIED") as exc_info:
        client.get("Book", "book-1")
    client_code = exc_info.value.code

    payload = _call(server, "get_object", {"obj_type": "Book", "obj_id": "book-1"})
    assert payload["error"]["type"] == "VisibilityError"
    assert payload["error"]["code"] == client_code == "VISIBILITY_DENIED"
    assert payload["error"]["kind"] == "visibility"


def test_parity_permission_denied_code() -> None:
    consumer = Consumer(
        actor_id="volunteer-1",
        role="Volunteer",
        scope_level="shelf",
        scope_id="shelf-1",
        kind="human",
    )
    client, server = _client_and_server(consumer)
    params = {"book_id": "book-1", "shelf_id": "shelf-2"}

    with raises_code(PermissionDenied, "PERMISSION_DENIED") as exc_info:
        client.execute("RelocateBook", params)
    client_code = exc_info.value.code

    payload = _call(server, "execute_action", {"api_name": "RelocateBook", "params": params})
    assert payload["error"]["type"] == "PermissionDenied"
    assert payload["error"]["code"] == client_code == "PERMISSION_DENIED"
    assert payload["error"]["kind"] == "permission"


def test_parity_precondition_failed_code() -> None:
    consumer = _library_librarian()
    client, server = _client_and_server(consumer)
    params = {"book_id": "does-not-exist", "shelf_id": "shelf-2"}

    with pytest.raises(ActionError) as exc_info:
        client.execute("RelocateBook", params)
    client_code = exc_info.value.code

    payload = _call(server, "execute_action", {"api_name": "RelocateBook", "params": params})
    assert payload["error"]["type"] == "PreconditionFailed"
    assert payload["error"]["code"] == client_code == "PRECONDITION_FAILED"
    assert payload["error"]["kind"] == "precondition"


def test_parity_invalid_params_code() -> None:
    consumer = _library_librarian()
    client, server = _client_and_server(consumer)
    params = {"book_id": "book-1", "shelf_id": 12345}  # wrong declared type

    with pytest.raises(ActionError) as exc_info:
        client.execute("RelocateBook", params)
    client_code = exc_info.value.code

    payload = _call(server, "execute_action", {"api_name": "RelocateBook", "params": params})
    assert payload["error"]["type"] == "ValidationFailed"
    assert payload["error"]["code"] == client_code == "INVALID_PARAMS"
    assert payload["error"]["kind"] == "validation"


def test_parity_cardinality_violation_code() -> None:
    """A `conflict`-kind refusal (`CARDINALITY_VIOLATION`): `onShelf` is
    declared `MANY_TO_ONE`, so a book already carrying an active `onShelf`
    link cannot get a second one. Reviewer-reproduced fixture: relocate
    book-1 once (creating its first `onShelf` link), then relocate it
    again -- client and server share the same store, so the second
    attempt via either surface trips the same cardinality conflict."""
    consumer = _library_librarian()
    client, server = _client_and_server(consumer)

    client.execute("RelocateBook", {"book_id": "book-1", "shelf_id": "shelf-2"})

    with raises_code(ConflictError, "CARDINALITY_VIOLATION") as exc_info:
        client.execute("RelocateBook", {"book_id": "book-1", "shelf_id": "shelf-1"})
    client_code = exc_info.value.code

    payload = _call(
        server,
        "execute_action",
        {"api_name": "RelocateBook", "params": {"book_id": "book-1", "shelf_id": "shelf-1"}},
    )
    assert payload["error"]["type"] == "ConflictError"
    assert payload["error"]["code"] == client_code == "CARDINALITY_VIOLATION"
    assert payload["error"]["kind"] == "conflict"


def _build_relabel_ontology() -> tuple[Ontology, type[OntologyObject]]:
    """`_base_library_ontology()` plus extra actions (`RelabelBook` and
    `ReturnNested`)
    whose handler writes directly to a source-backed `Book` property from
    inside the write-capture context -- an authority refusal fixture, kept
    separate from the shared base ontology so `test_list_action_types_
    matches_registry` (which asserts the exact `{"RelocateBook"}` action
    set) is unaffected."""
    ontology, Book = _base_library_ontology()

    class _RelabelBookParams(ActionParams):
        book_id: str = target(Book)

    @ontology.action(
        _RelabelBookParams,
        target=Book,
        roles=["Librarian"],
        display_name="Relabel Book",
        description=(
            "Directly update a source-backed Book property -- an "
            "authority refusal fixture (declared-contracts §3 AC2)"
        ),
        api_name="RelabelBook",
    )
    def _relabel(ctx: ActionContext, params: _RelabelBookParams) -> dict[str, str]:
        # `Book.title` is not declared ontology-owned, so this write is
        # refused (UNDECLARED_SOURCE_WRITE) before it ever reaches the store.
        book = ctx.get(Book, params.book_id)
        assert book is not None
        book.title = "Relabeled"
        ctx.save(book)
        return {"book_id": params.book_id}

    class _ReturnNestedParams(ActionParams):
        pass

    @ontology.action(
        _ReturnNestedParams,
        target=Book,
        roles=["Librarian"],
        display_name="Return Nested",
        description="Return a nested JSON-safe result",
        api_name="ReturnNested",
    )
    def _return_nested(
        _ctx: ActionContext, _params: _ReturnNestedParams
    ) -> dict[str, Any]:
        return {"nested": {"a": [1, "b", None]}}

    return ontology, Book


def _client_and_server_with_relabel(consumer: Consumer) -> tuple[OntologyClient, MCPServer]:
    ontology, _Book = _build_relabel_ontology()
    ontology.validate()
    store = ObjectStore(ontology.registry)
    store.insert("Library", {"id": "lib-1"}, SRC)
    store.insert("Shelf", {"id": "shelf-1", "library_id": "lib-1"}, SRC)
    store.create_link("inLibrary", "shelf-1", "lib-1")
    store.insert(
        "Book", {"id": "book-1", "shelf_id": "shelf-1", "title": "Foundry"}, SRC
    )

    client = OntologyClient(ontology, store, consumer)

    server_ontology, _ = _build_relabel_ontology()
    server_ontology.validate()
    server = build_mcp_server(server_ontology, store, consumer)
    return client, server


def test_parity_undeclared_source_write_code() -> None:
    """An `authority`-kind refusal (`UNDECLARED_SOURCE_WRITE`): a handler
    attempting to write a source-backed property from inside the
    execute()-owned write-capture context is refused by `ObjectStore.update`
    itself, on both the client and the MCP surface."""
    consumer = _library_librarian()
    client, server = _client_and_server_with_relabel(consumer)
    params = {"book_id": "book-1"}

    with raises_code(AuthorityError, "UNDECLARED_SOURCE_WRITE") as exc_info:
        client.execute("RelabelBook", params)
    client_code = exc_info.value.code

    payload = _call(server, "execute_action", {"api_name": "RelabelBook", "params": params})
    assert payload["error"]["type"] == "AuthorityError"
    assert payload["error"]["code"] == client_code == "UNDECLARED_SOURCE_WRITE"
    assert payload["error"]["kind"] == "authority"


def test_nested_action_result_reaches_client_and_mcp() -> None:
    consumer = _library_librarian()
    client, server = _client_and_server_with_relabel(consumer)
    expected = {"nested": {"a": [1, "b", None]}}

    assert client.execute("ReturnNested", {}) == expected
    assert _call(
        server, "execute_action", {"api_name": "ReturnNested", "params": {}}
    ) == {"result": expected}


# -- JSON result boundary (#167) ---------------------------------------------

_DATETIME_RESULT_EXPECTED = {
    "next_free": "2026-10-07T03:00:00+00:00",
    "slots": [{"starts_at": "2026-10-06T09:00:00+00:00"}, {"day": "2026-10-06"}],
}


def _build_result_boundary_ontology() -> Ontology:
    """`_base_library_ontology()` plus Functions and Actions whose handlers
    return values at the JSON boundary: datetimes/dates (encoded), and
    unencodable results (refused with `RESULT_NOT_JSON`)."""
    ontology, Book = _base_library_ontology()

    def _datetime_result() -> dict[str, Any]:
        return {
            "next_free": datetime(2026, 10, 7, 3, 0, tzinfo=UTC),
            "slots": [
                {"starts_at": datetime(2026, 10, 6, 9, 0, tzinfo=UTC)},
                {"day": date(2026, 10, 6)},
            ],
        }

    @ontology.function(api_name="nextFreeSlots")
    def _next_free_slots(query: BoundQuery) -> dict[str, Any]:
        return _datetime_result()

    @ontology.function(api_name="setRows")
    def _set_rows(query: BoundQuery) -> dict[str, Any]:
        return {"rows": [{"when": {1, 2}}]}

    @ontology.function(api_name="bareFloat")
    def _bare_float(query: BoundQuery) -> float:
        return 1.5

    def _declare_action(api_name: str, handler: Any) -> None:
        # One params class per action (a params class may be declared once).
        params_cls = type(f"_{api_name}Params", (ActionParams,), {})
        ontology.action(
            params_cls,
            target=Book,
            roles=["Librarian"],
            display_name=api_name,
            description="Return a value at the JSON boundary",
            api_name=api_name,
        )(handler)

    def _slots_handler(_ctx: ActionContext, _params: Any) -> dict[str, Any]:
        return _datetime_result()

    def _inf_handler(_ctx: ActionContext, _params: Any) -> dict[str, Any]:
        return {"value": float("inf")}

    def _list_handler(_ctx: ActionContext, _params: Any) -> Any:
        return [1, 2]

    _declare_action("BookSlots", _slots_handler)
    _declare_action("BookInf", _inf_handler)
    _declare_action("BookList", _list_handler)
    return ontology


def _result_boundary_client_and_server() -> tuple[OntologyClient, MCPServer]:
    consumer = _library_librarian()
    ontology = _build_result_boundary_ontology()
    ontology.validate()
    store = ObjectStore(ontology.registry)
    store.insert("Library", {"id": "lib-1"}, SRC)
    store.insert("Shelf", {"id": "shelf-1", "library_id": "lib-1"}, SRC)
    store.create_link("inLibrary", "shelf-1", "lib-1")
    store.insert("Book", {"id": "book-1", "shelf_id": "shelf-1"}, SRC)
    client = OntologyClient(ontology, store, consumer)

    server_ontology = _build_result_boundary_ontology()
    server_ontology.validate()
    return client, build_mcp_server(server_ontology, store, consumer)


def _result_not_json(message: str) -> dict[str, Any]:
    return {
        "error": {
            "type": "PreconditionFailed",
            "message": message,
            "code": "RESULT_NOT_JSON",
            "kind": "precondition",
        }
    }


def test_function_datetime_result_is_iso_strings_on_client_and_mcp() -> None:
    client, server = _result_boundary_client_and_server()

    in_process = client.call_function("nextFreeSlots", {})
    payload = _call(server, "call_function", {"api_name": "nextFreeSlots", "params": {}})

    assert in_process == _DATETIME_RESULT_EXPECTED
    assert payload == {"result": _DATETIME_RESULT_EXPECTED}
    assert payload["result"] == in_process


def test_action_datetime_result_is_iso_strings_on_client_and_mcp() -> None:
    client, server = _result_boundary_client_and_server()

    in_process = client.execute("BookSlots", {})
    payload = _call(server, "execute_action", {"api_name": "BookSlots", "params": {}})

    assert in_process == _DATETIME_RESULT_EXPECTED
    assert payload == {"result": _DATETIME_RESULT_EXPECTED}
    assert payload["result"] == in_process


def test_function_unencodable_result_is_a_result_not_json_envelope() -> None:
    _client, server = _result_boundary_client_and_server()

    payload = _call(server, "call_function", {"api_name": "setRows", "params": {}})

    assert payload == _result_not_json(
        "'setRows': result is not JSON-serializable at "
        'result["rows"][0]["when"] (set)'
    )


def test_action_unencodable_result_is_a_result_not_json_envelope() -> None:
    _client, server = _result_boundary_client_and_server()

    payload = _call(server, "execute_action", {"api_name": "BookInf", "params": {}})

    assert payload == _result_not_json(
        "'BookInf': result is not JSON-serializable at "
        'result["value"] (non-finite float)'
    )


def test_action_list_result_is_a_result_not_json_envelope() -> None:
    _client, server = _result_boundary_client_and_server()

    payload = _call(server, "execute_action", {"api_name": "BookList", "params": {}})

    assert payload == _result_not_json(
        "'BookList': handler returned list; action results must be JSON-safe dictionaries"
    )


def test_function_bare_float_result_succeeds_on_mcp() -> None:
    _client, server = _result_boundary_client_and_server()

    payload = _call(server, "call_function", {"api_name": "bareFloat", "params": {}})

    assert payload == {"result": 1.5}


# -- optional-extra ergonomics (M6 step 3) -----------------------------------


def test_missing_mcp_extra_names_the_install_command() -> None:
    """`pip install ontary && ontary-mcp` used to raise a bare
    `ModuleNotFoundError: No module named 'mcp'` -- the console script ships
    with the CORE package while the server needs an extra. Found by installing
    the wheel into an empty venv, which nothing in CI had ever done."""
    ontology, _Book = _base_library_ontology()
    ontology.validate()
    store = ObjectStore(ontology.registry)
    consumer = _librarian()

    with _mcp_uninstalled():
        with pytest.raises(ImportError) as exc_info:
            build_mcp_server(ontology, store, consumer)

    message = str(exc_info.value)
    assert "optional `mcp` dependency" in message
    assert "unsupported" not in message
    # The cause is preserved, so the real import failure is still diagnosable.
    assert isinstance(exc_info.value.__cause__, ModuleNotFoundError)


def test_root_reexports_the_mcp_server_class() -> None:
    """`from ontary import MCPServer` is the class the builders return (#61)."""
    import ontary

    assert ontary.MCPServer is MCPServer
    assert "MCPServer" in ontary.__all__


def test_root_mcp_server_without_the_extra_names_the_install_command() -> None:
    """Without `mcp`, only touching `ontary.MCPServer` fails, with the
    builders' install hint (#61). A star import fetches every `__all__` name,
    so in a core-only install it raises the same hint (accepted, documented)."""
    import ontary

    with _mcp_uninstalled():
        with pytest.raises(ImportError) as attr_info:
            ontary.MCPServer  # noqa: B018 -- the attribute access is the test
        with pytest.raises(ImportError) as star_info:
            exec("from ontary import *", {})

    for exc_info in (attr_info, star_info):
        assert str(exc_info.value) == MCP_EXTRA_HINT
        assert isinstance(exc_info.value.__cause__, ModuleNotFoundError)


def test_root_mcp_server_with_mcp_1x_layout_reports_incompatible_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import ontary

    monkeypatch.setitem(sys.modules, "mcp.server.mcpserver", None)

    with pytest.raises(ImportError) as exc_info:
        ontary.MCPServer  # noqa: B018 -- the attribute access is the test

    assert str(exc_info.value) == MCP_INCOMPATIBLE_HINT


def test_root_getattr_keeps_the_normal_error_for_unknown_names() -> None:
    import ontary

    with pytest.raises(AttributeError, match="NoSuchName"):
        ontary.NoSuchName  # noqa: B018 -- the attribute access is the test


def test_entrypoint_reports_the_missing_extra_before_the_wiring_hint() -> None:
    with _mcp_uninstalled():
        with pytest.raises(SystemExit) as exc_info:
            main()

    message = str(exc_info.value)
    assert "optional `mcp` dependency" in message
    assert "unsupported" not in message


def test_entrypoint_explains_the_wiring_when_the_extra_is_present() -> None:
    with pytest.raises(SystemExit) as exc_info:
        main()

    message = str(exc_info.value)
    assert "no default ontology to serve" in message
    assert "ontary[mcp]" not in message


def test_a_broken_mcp_install_is_not_reported_as_a_missing_extra() -> None:
    """If `mcp` is installed but ITS OWN dependency is missing, the original
    error must survive: telling someone to install the extra they already have
    would send them down the wrong path."""

    class _BreakInsideMCP:
        def find_spec(
            self, fullname: str, path: object = None, target: object = None
        ) -> None:
            if fullname == "mcp" or fullname.startswith("mcp."):
                raise ModuleNotFoundError(
                    "No module named 'anyio'", name="anyio"
                )
            return None

    cached = {
        name: module
        for name, module in sys.modules.items()
        if name == "mcp" or name.startswith("mcp.")
    }
    for name in cached:
        del sys.modules[name]
    finder = _BreakInsideMCP()
    sys.meta_path.insert(0, finder)
    try:
        with pytest.raises(ModuleNotFoundError) as exc_info:
            _load_mcp_server()
    finally:
        sys.meta_path.remove(finder)
        sys.modules.update(cached)

    assert exc_info.value.name == "anyio"
    assert "ontary[mcp]" not in str(exc_info.value)


def test_try_fails_closed_when_envelope_construction_itself_raises() -> None:
    """`_try` never leaks in place of an envelope.

    `_error` runs INSIDE `_try`'s `except`, so anything it raises bypasses
    the sibling `except Exception` and reaches the caller with no envelope at
    all -- defeating this module's "never a partial" promise. Two ways in:
    a consumer subclass that forwards only the message and so carries no
    `.code`, and an `OntaryError` whose `kind` is outside `_KIND_NAMES`.
    """
    from ontary.errors import ValidationFailed
    from ontary.mcp_server import _try

    class NoCode(ValidationFailed):
        def __init__(self, message: str) -> None:
            Exception.__init__(self, message)

    def raises_no_code() -> None:
        raise NoCode("carries no code")

    def raises_unknown_kind() -> None:
        exc = ValidationFailed("odd kind", code="INVALID_PARAMS")
        exc.kind = "not-a-kind"  # type: ignore[assignment]
        raise exc

    for failing in (raises_no_code, raises_unknown_kind):
        result, envelope = _try(failing)
        assert result is None
        assert envelope is not None
        assert envelope["error"]["code"] == "INTERNAL_ERROR"
        assert envelope["error"]["kind"] == "internal"


# -- documented tool arguments ---------------------------------------------------

_DOCS = Path(__file__).resolve().parents[1] / "docs"


def _documented_tool_arguments(
    page: str,
) -> dict[str, tuple[set[str], set[str], dict[str, Any]]]:
    """Parse the api-mcp arguments table: tool -> (required, optional, defaults)."""
    text = (_DOCS / page).read_text()
    rows: dict[str, tuple[set[str], set[str], dict[str, Any]]] = {}
    for line in text.splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) != 3 or not re.fullmatch(r"`\w+`(, `\w+`)*", cells[0]):
            continue
        if cells[1] != "—" and not cells[1].startswith("`"):
            continue
        required = set(re.findall(r"`(\w+)`", cells[1]))
        # Only names that open the cell or follow a comma are arguments; a
        # backticked value inside "(default ...)" is that argument's default.
        optional = set(re.findall(r"(?:^|, )`(\w+)`", cells[2]))
        defaults = {
            name: json.loads(value)
            for name, value in re.findall(r"`(\w+)` [(（][^`]*`([^`]+)`[)）]", cells[2])
        }
        for tool in re.findall(r"`(\w+)`", cells[0]):
            assert tool not in rows, f"{page}: {tool} appears in two rows"
            rows[tool] = (required, optional, defaults)
    return rows


@pytest.mark.parametrize("page", ["api-mcp.md", "api-mcp.ja.md"])
def test_documented_tool_arguments_match_the_server(page: str) -> None:
    """The api-mcp arguments table lists every tool and its schema arguments.
    Each required argument is refused when missing; the optional ones may be
    omitted; documented defaults equal the schema's."""
    server, _store = _build_server(_librarian())
    documented = _documented_tool_arguments(page)
    tools = asyncio.run(server.list_tools())
    assert set(documented) == {t.name for t in tools}

    def only(names: set[str]) -> dict[str, Any]:
        return {name: ({} if name in ("params", "where") else "x") for name in names}

    for tool in tools:
        required, optional, defaults = documented[tool.name]
        properties = tool.input_schema.get("properties", {})
        assert required | optional == set(properties), tool.name
        assert not required & optional, tool.name
        for name, value in defaults.items():
            assert name in optional, (tool.name, name)
            assert properties[name].get("default") == value, (tool.name, name)
        for name in optional:
            if "default" in properties[name] and properties[name]["default"] is not None:
                assert name in defaults, f"{page}: {tool.name}.{name} default undocumented"

        error = _call(server, tool.name, only(required)).get("error")
        message = "" if error is None else error["message"]
        assert "missing required tool parameter" not in message, (tool.name, message)
        for missing in required:
            error = _call(server, tool.name, only(required - {missing}))["error"]
            assert error["code"] == "INVALID_PARAMS", (tool.name, missing)
            assert f"missing required tool parameter {missing!r}" in error["message"]
