"""Integration tests for `ontary.client.OntologyClient` over the full
client surface (spec AC6, §8 "Client facade" row): get/list/traverse/
aggregate/execute/call_function/ingest/ingest_links, on a toy "library"
ontology matching the shape used by `test_query.py`/`test_actions.py`.

Also covers:
- a Function receiving only a `BoundQuery` (never the store) -- it cannot
  write, and its reads are subject to the same redaction/visibility guards
  as any other consumer read (AC3).
- registering a handler for an undeclared function raising (AC3's
  "declared? then bindable" rule mirrors `ActionExecutor`).
- two independent `OntologyDef`s + stores + clients coexisting in one
  process without cross-talk (spec §7) -- a second, deliberately different
  toy domain ("Kanban": Board/Card).
- a denied read carrying `VISIBILITY_DENIED` propagating unchanged through the
  client, exactly as it does through a bare `GuardedQuery` (AC6/AC7).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest
from conftest import raises_code

from ontary.actions import ActionError
from ontary.client import OntologyClient
from ontary.errors import PreconditionFailed, ValidationFailed, VisibilityError
from ontary.functions import BoundQuery, FunctionRegistry
from ontary.meta import (
    ActionParameterDef,
    ActionTypeDef,
    Cardinality,
    FunctionDef,
    LinkTypeDef,
    ObjectTypeDef,
    OntologyRegistry,
    PropertyDef,
    Sensitivity,
)
from ontary.ontology import OntologyDef
from ontary.scope import DirectProperty, ScopePolicy, SelfScope, ViaLink
from ontary.security import Consumer
from ontary.store import ObjectStore, Source

LEVELS = ["shelf", "library"]
SRC = Source(source_system="test")


RegistryFactory = Callable[..., OntologyRegistry]
PolicyFactory = Callable[..., ScopePolicy]


def _library_registry(make_registry: RegistryFactory) -> OntologyRegistry:
    return make_registry(
        object_types=[
            ObjectTypeDef(
                api_name="Library",
                display_name="Library",
                description="A library building",
                layer="L0",
                properties=[PropertyDef(name="id", type="str")],
                primary_key="id",
            ),
            ObjectTypeDef(
                api_name="Shelf",
                display_name="Shelf",
                description="A shelf inside a library",
                layer="L0",
                properties=[PropertyDef(name="id", type="str")],
                primary_key="id",
            ),
            ObjectTypeDef(
                api_name="Book",
                display_name="Book",
                description="A book, shelved somewhere",
                layer="L0",
                properties=[
                    PropertyDef(name="id", type="str"),
                    PropertyDef(name="shelf_id", type="str", required=False),
                    PropertyDef(name="title", type="str", required=False),
                    PropertyDef(name="pages", type="int", required=False),
                    # ai_usable=False: internal-only field, never surfaced to an
                    # AI consumer (exercised by the function redaction test).
                    PropertyDef(
                        name="internal_note",
                        type="str",
                        required=False,
                        sensitivity=Sensitivity(ai_usable=False, human_visible=True),
                    ),
                ],
                primary_key="id",
            ),
        ],
        link_types=[
            LinkTypeDef(
                api_name="inLibrary",
                from_type="Shelf",
                to_type="Library",
                cardinality=Cardinality.MANY_TO_ONE,
                description="Shelf -> its library",
            ),
            LinkTypeDef(
                api_name="onShelf",
                from_type="Book",
                to_type="Shelf",
                cardinality=Cardinality.MANY_TO_ONE,
                description="Book -> its shelf",
                # Declared ontology-owned (declared-contracts §3 AC1):
                # RelocateBook's handler creates this link from inside
                # ActionExecutor.execute()'s write-capture context.
                owned=True,
            ),
            LinkTypeDef(
                api_name="relatedShelf",
                from_type="Book",
                to_type="Shelf",
                cardinality=Cardinality.MANY_TO_MANY,
                description="Book -> related shelves",
            ),
        ],
        action_types=[
            ActionTypeDef(
                api_name="RelocateBook",
                display_name="Relocate Book",
                target_type="Book",
                executable_by_roles=["Librarian"],
                description="Move a book to a different shelf",
                parameters=[
                    ActionParameterDef(
                        name="book_id",
                        type="str",
                        refers_to="Book",
                        scope_semantics="target",
                    ),
                    ActionParameterDef(
                        name="shelf_id",
                        type="str",
                        refers_to="Shelf",
                        scope_semantics="scope",
                    ),
                ],
            ),
            ActionTypeDef(
                api_name="ReturnNested",
                display_name="Return Nested",
                target_type="Book",
                executable_by_roles=["Librarian"],
                description="Return a nested JSON-safe result",
                parameters=[],
            ),
        ],
        functions=[
            FunctionDef(
                api_name="countBooksOnShelf",
                description="Count the (visible) books on a shelf",
                input_description="shelf_id",
                output_description="int count",
            ),
            FunctionDef(
                api_name="internalNotes",
                description="Return each visible book's internal_note field",
                input_description="none",
                output_description="list of str | None",
            ),
        ],
    )


def _library_policy(make_policy: PolicyFactory) -> ScopePolicy:
    return make_policy(
        levels=LEVELS,
        rules={
            "Library": [SelfScope(level="library")],
            "Shelf": [
                SelfScope(level="shelf"),
                ViaLink(
                    link_api_name="inLibrary", direction="from", parent_type="Library"
                ),
            ],
            "Book": [
                DirectProperty(level="shelf", property_name="shelf_id"),
                ViaLink(
                    link_api_name="onShelf", direction="from", parent_type="Shelf"
                ),
            ],
        },
        min_n=1,
    )


def _relocate_handler(store: ObjectStore) -> Any:
    def _handler(consumer: Consumer, params: dict[str, Any]) -> dict[str, str]:
        book = store.read_current("Book", params["book_id"])
        if book is None:
            raise ActionError(f"book {params['book_id']!r} does not exist", code="PRECONDITION_FAILED")
        with store.transaction():
            store.create_link("onShelf", params["book_id"], params["shelf_id"])
        return {"book_id": params["book_id"]}

    return _handler


def _return_nested_handler(
    consumer: Consumer, params: dict[str, Any]
) -> dict[str, Any]:
    return {"nested": {"a": [1, "b", None]}}


def _count_books_on_shelf(query: BoundQuery, params: dict[str, Any]) -> int:
    return len(
        query.list(
            "Book", where={"shelf_id": params["shelf_id"]}, limit=None
        )
    )


def _librarian(scope_id: str = "shelf-1") -> Consumer:
    return Consumer(
        actor_id="lib-1",
        role="Librarian",
        scope_level="shelf",
        scope_id=scope_id,
        kind="human",
    )


def _ai_librarian(scope_id: str = "shelf-1") -> Consumer:
    return Consumer(
        actor_id="ai-1",
        role="Librarian",
        scope_level="shelf",
        scope_id=scope_id,
        kind="ai",
    )


def _library_librarian(scope_id: str = "lib-1") -> Consumer:
    """A librarian scoped at the broader `library` level -- covers every
    shelf in that library, unlike `_librarian` (shelf-scoped)."""
    return Consumer(
        actor_id="head-lib-1",
        role="Librarian",
        scope_level="library",
        scope_id=scope_id,
        kind="human",
    )


def _build_client(
    consumer: Consumer,
    make_registry: RegistryFactory,
    make_policy: PolicyFactory,
) -> tuple[OntologyClient, ObjectStore]:
    registry = _library_registry(make_registry)
    functions = FunctionRegistry(registry)
    functions.register("countBooksOnShelf", _count_books_on_shelf)
    ontology = OntologyDef(
        name="library",
        registry=registry,
        policy=_library_policy(make_policy),
        functions=functions,
    )
    ontology.validate()
    store = ObjectStore(registry)
    store.insert("Library", {"id": "lib-1"}, SRC)
    store.insert("Shelf", {"id": "shelf-1", "library_id": "lib-1"}, SRC)
    store.create_link("inLibrary", "shelf-1", "lib-1")
    store.insert(
        "Book",
        {
            "id": "book-1",
            "shelf_id": "shelf-1",
            "title": "Foundry",
            "pages": 100,
            "internal_note": "shhh",
        },
        SRC,
    )
    client = OntologyClient(ontology, store, consumer)
    client.actions._register("RelocateBook", _relocate_handler(store))
    client.actions._register("ReturnNested", _return_nested_handler)
    return client, store


# -- reads --------------------------------------------------------------


def test_get_returns_object(
    make_registry: RegistryFactory,
    make_policy: PolicyFactory,
) -> None:
    client, _ = _build_client(_librarian(), make_registry, make_policy)
    obj = client.get("Book", "book-1")
    assert obj is not None
    assert obj.payload["title"] == "Foundry"


def test_list_returns_visible_objects(
    make_registry: RegistryFactory,
    make_policy: PolicyFactory,
) -> None:
    client, _ = _build_client(_librarian(), make_registry, make_policy)
    rows = client.list("Book", limit=None)
    assert [r.payload["id"] for r in rows] == ["book-1"]


def test_traverse_follows_declared_link(
    make_registry: RegistryFactory,
    make_policy: PolicyFactory,
) -> None:
    # A shelf-scoped consumer can't see the owning Library object itself
    # (deny-by-default: Library has no rule resolving the narrower "shelf"
    # level) -- use a library-scoped consumer, which covers it.
    client, _ = _build_client(_library_librarian(), make_registry, make_policy)
    rows = client.traverse("Shelf", "inLibrary", "shelf-1")
    assert [r.payload["id"] for r in rows] == ["lib-1"]


def test_traverse_reverse_flag_preserves_link_direction(
    make_registry: RegistryFactory,
    make_policy: PolicyFactory,
) -> None:
    client, store = _build_client(_library_librarian(), make_registry, make_policy)
    store.insert("Shelf", {"id": "shelf-2", "library_id": "lib-1"}, SRC)
    store.create_link("inLibrary", "shelf-2", "lib-1")
    store.insert("Book", {"id": "book-c", "shelf_id": "shelf-2"}, SRC)
    store.create_link("relatedShelf", "book-1", "shelf-1")
    store.create_link("relatedShelf", "book-c", "shelf-1")
    store.create_link("relatedShelf", "book-1", "shelf-2")

    forward = client.traverse("Book", "relatedShelf", "book-1")
    reverse = client.traverse("Shelf", "relatedShelf", "shelf-1", reverse=True)
    assert {row.payload["id"] for row in forward} == {"shelf-1", "shelf-2"}
    assert {row.payload["id"] for row in reverse} == {"book-1", "book-c"}


def test_traverse_rejects_wrong_source_type(
    make_registry: RegistryFactory,
    make_policy: PolicyFactory,
) -> None:
    """`OntologyClient.traverse`'s from-type mismatch is a coded
    a coded `UNKNOWN_NAME` error, not a bare `ValueError` (m35-sdk-refactor
    T6/AC7)."""
    client, _ = _build_client(_librarian(), make_registry, make_policy)
    with raises_code(ValidationFailed, "UNKNOWN_NAME"):
        client.traverse("Book", "inLibrary", "book-1")


def test_traverse_unknown_link_raises_unknown_name(
    make_registry: RegistryFactory,
    make_policy: PolicyFactory,
) -> None:
    client, _ = _build_client(_librarian(), make_registry, make_policy)

    with raises_code(ValidationFailed, "UNKNOWN_NAME"):
        client.traverse("Book", "noSuchLink", "book-1")


def test_aggregate_delegates_to_guarded_query(
    make_registry: RegistryFactory,
    make_policy: PolicyFactory,
) -> None:
    client, store = _build_client(_librarian(), make_registry, make_policy)
    store.insert(
        "Book", {"id": "book-2", "shelf_id": "shelf-1", "pages": 200}, SRC
    )
    mean_pages = client.aggregate("Book", "pages")
    assert mean_pages == 150.0


def test_aggregate_func_delegates_to_guarded_query(
    make_registry: RegistryFactory,
    make_policy: PolicyFactory,
) -> None:
    client, store = _build_client(_librarian(), make_registry, make_policy)
    store.insert("Book", {"id": "book-2", "shelf_id": "shelf-1", "pages": 200}, SRC)

    assert client.aggregate("Book", "pages", func="count") == 2
    assert client.aggregate("Book", "pages", func="sum") == pytest.approx(300.0)


def test_aggregate_by_delegates_and_respects_where(
    make_registry: RegistryFactory,
    make_policy: PolicyFactory,
) -> None:
    """Mutation receipt (pagination-hardening T3 review P1): dropping
    `where=` from `OntologyClient.aggregate_by`'s string-form delegation to
    `GuardedQuery.aggregate_by` left every prior test green -- assert the
    FILTERED dict's actual values, not merely its shape, so a dropped/
    mis-passed `where=` widens the aggregate past the caller's filter and
    is caught here."""
    client, store = _build_client(_library_librarian(), make_registry, make_policy)
    store.insert("Shelf", {"id": "shelf-2", "library_id": "lib-1"}, SRC)
    store.create_link("inLibrary", "shelf-2", "lib-1")
    store.insert(
        "Book",
        {"id": "book-2", "shelf_id": "shelf-1", "title": "Other", "pages": 300},
        SRC,
    )
    store.insert(
        "Book",
        {"id": "book-3", "shelf_id": "shelf-2", "title": "Foundish", "pages": 50},
        SRC,
    )

    unfiltered = client.aggregate_by("Book", "pages", "shelf_id")
    assert unfiltered == {
        "shelf-1": pytest.approx(200.0),
        "shelf-2": pytest.approx(50.0),
    }

    filtered = client.aggregate_by(
        "Book", "pages", "shelf_id", where={"title": "Foundry"}
    )
    assert filtered == {"shelf-1": pytest.approx(100.0)}

    maximum = client.aggregate_by("Book", "pages", "shelf_id", func="max")
    assert maximum == {
        "shelf-1": pytest.approx(300.0),
        "shelf-2": pytest.approx(50.0),
    }


def test_aggregate_by_rejects_empty_group_by(
    make_registry: RegistryFactory,
    make_policy: PolicyFactory,
) -> None:
    client, _ = _build_client(_librarian(), make_registry, make_policy)
    with raises_code(ValidationFailed, "INVALID_GROUP_BY"):
        client.aggregate_by("Book", "pages", "")


def test_client_read_denial_mirrors_guarded_query(
    make_registry: RegistryFactory,
    make_policy: PolicyFactory,
) -> None:
    client, _ = _build_client(
        _librarian(scope_id="shelf-OTHER"), make_registry, make_policy
    )
    with raises_code(VisibilityError, "VISIBILITY_DENIED"):
        client.get("Book", "book-1")


# -- writes ---------------------------------------------------------------


def test_execute_runs_registered_handler(
    make_registry: RegistryFactory,
    make_policy: PolicyFactory,
) -> None:
    # A shelf-scoped consumer can only ever cover its own shelf; relocating
    # a book to a *different* shelf requires a consumer whose scope covers
    # both (library-scoped here).
    client, store = _build_client(_library_librarian(), make_registry, make_policy)
    store.insert("Shelf", {"id": "shelf-2", "library_id": "lib-1"}, SRC)
    store.create_link("inLibrary", "shelf-2", "lib-1")
    result = client.execute(
        "RelocateBook", {"book_id": "book-1", "shelf_id": "shelf-2"}
    )
    assert result == {"book_id": "book-1"}


def test_execute_string_form_accepts_keyword_arguments(
    make_registry: RegistryFactory,
    make_policy: PolicyFactory,
) -> None:
    client, store = _build_client(_library_librarian(), make_registry, make_policy)
    store.insert("Shelf", {"id": "shelf-2", "library_id": "lib-1"}, SRC)
    store.create_link("inLibrary", "shelf-2", "lib-1")

    result = client.execute(
        action="RelocateBook",
        params={"book_id": "book-1", "shelf_id": "shelf-2"},
    )

    assert result == {"book_id": "book-1"}


def test_execute_nested_result_reaches_client_without_audit_persistence(
    make_registry: RegistryFactory,
    make_policy: PolicyFactory,
) -> None:
    client, store = _build_client(_library_librarian(), make_registry, make_policy)
    expected = {"nested": {"a": [1, "b", None]}}

    assert client.execute(action="ReturnNested", params={}) == expected
    assert not hasattr(store.audit_entries()[-1], "result")


def test_execute_refuses_missing_string_form_params(
    make_registry: RegistryFactory,
    make_policy: PolicyFactory,
) -> None:
    client, _ = _build_client(_librarian(), make_registry, make_policy)
    untyped_execute: Any = client.execute

    with raises_code(ValidationFailed, "INVALID_PARAMS"):
        untyped_execute("RelocateBook")


def test_traverse_refuses_missing_string_form_from_id(
    make_registry: RegistryFactory,
    make_policy: PolicyFactory,
) -> None:
    client, _ = _build_client(_librarian(), make_registry, make_policy)
    untyped_traverse: Any = client.traverse

    with raises_code(ValidationFailed, "INVALID_PARAMS"):
        untyped_traverse("Book", "bookShelf")


# -- declarations (spec AC10) -------------------------------------------


def test_client_declarations_matches_ontology_policy_min_n(
    make_registry: RegistryFactory,
    make_policy: PolicyFactory,
) -> None:
    client, _ = _build_client(_librarian(), make_registry, make_policy)
    decls = client.declarations
    assert decls.min_n == 1  # this fixture's `_library_policy()` declares min_n=1


def test_ingest_bulk_upserts_records(
    make_registry: RegistryFactory,
    make_policy: PolicyFactory,
) -> None:
    client, _ = _build_client(_librarian(), make_registry, make_policy)
    report = client.ingest(
        "Book",
        [{"id": "book-3", "shelf_id": "shelf-1", "title": "Ingested"}],
        Source(source_system="loader"),
    )
    assert report.ok
    assert client.get("Book", "book-3") is not None


def test_ingest_links_bulk_creates_links(
    make_registry: RegistryFactory,
    make_policy: PolicyFactory,
) -> None:
    client, store = _build_client(_librarian(), make_registry, make_policy)
    store.insert("Shelf", {"id": "shelf-3", "library_id": "lib-1"}, SRC)
    report = client.ingest_links(
        "inLibrary", [("shelf-3", "lib-1")], Source(source_system="loader")
    )
    assert report.ok


# -- functions --------------------------------------------------------------


def test_call_function_reads_through_guarded_query(
    make_registry: RegistryFactory,
    make_policy: PolicyFactory,
) -> None:
    client, _ = _build_client(_librarian(), make_registry, make_policy)
    count = client.call_function("countBooksOnShelf", {"shelf_id": "shelf-1"})
    assert count == 1


def test_function_cannot_write_by_construction() -> None:
    """A registered Function handler's type is `(BoundQuery, dict) -> Any`;
    `BoundQuery` exposes no store/write methods at all -- this is enforced
    structurally, not by a runtime check.

    `capability` joined the surface in M5 (spec `governed-effects` AC6) and is
    a READ of the outside world, not a write: a Function may reach an LLM or an
    HTTP GET, but it still cannot declare or emit an effect (AC4 refuses
    `@ontology.function(effects=...)` outright) and its `BoundQuery` carries no
    effect dispatchers at all (AC5b). So "a Function cannot write" holds for
    the ontology AND for the outside world.
    """
    public_methods = {
        name for name in dir(BoundQuery) if not name.startswith("_")
    }
    assert public_methods == {
        "capability",
        "count",
        "exists",
        "get",
        "list",
        "traverse",
        "aggregate",
        "aggregate_by",
        # A READ of a number the engine already derives to enforce min-N.
        # It returns an int, never an identity, and is itself min-N gated --
        # so it widens what a Function can LEARN by exactly one aggregate
        # statistic, and not what it can DO.
        "count_contributors",
    }


def test_function_redaction_applies_through_bound_query(
    make_registry: RegistryFactory,
    make_policy: PolicyFactory,
) -> None:
    registry = _library_registry(make_registry)

    def _internal_notes(query: BoundQuery, params: dict[str, Any]) -> list[Any]:
        books = query.list("Book", limit=None)
        return [b.payload.get("internal_note") for b in books]

    functions = FunctionRegistry(registry)
    functions.register("countBooksOnShelf", _count_books_on_shelf)
    functions.register("internalNotes", _internal_notes)
    ontology = OntologyDef(
        name="library",
        registry=registry,
        policy=_library_policy(make_policy),
        functions=functions,
    )
    store = ObjectStore(registry)
    store.insert("Library", {"id": "lib-1"}, SRC)
    store.insert("Shelf", {"id": "shelf-1", "library_id": "lib-1"}, SRC)
    store.create_link("inLibrary", "shelf-1", "lib-1")
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

    ai_client = OntologyClient(ontology, store, _ai_librarian())
    notes = ai_client.call_function("internalNotes", {})
    assert notes == [None]  # ai_usable=False field redacted before the function saw it

    human_client = OntologyClient(ontology, store, _librarian())
    notes = human_client.call_function("internalNotes", {})
    assert notes == ["shhh"]


def test_register_undeclared_function_errors(
    make_registry: RegistryFactory,
) -> None:
    registry = _library_registry(make_registry)
    functions = FunctionRegistry(registry)
    with raises_code(PreconditionFailed, "FUNCTION_ERROR"):
        functions.register("notDeclared", _count_books_on_shelf)


def test_double_register_function_errors(
    make_registry: RegistryFactory,
) -> None:
    registry = _library_registry(make_registry)
    functions = FunctionRegistry(registry)
    functions.register("countBooksOnShelf", _count_books_on_shelf)
    with raises_code(PreconditionFailed, "FUNCTION_ERROR"):
        functions.register("countBooksOnShelf", _count_books_on_shelf)


# -- two ontologies coexisting (spec §7) -------------------------------


def _kanban_registry(make_registry: RegistryFactory) -> OntologyRegistry:
    return make_registry(
        object_types=[
            ObjectTypeDef(
                api_name="Board",
                display_name="Board",
                description="A kanban board",
                layer="L0",
                properties=[PropertyDef(name="id", type="str")],
                primary_key="id",
            ),
            ObjectTypeDef(
                api_name="Card",
                display_name="Card",
                description="A card on a board",
                layer="L0",
                properties=[
                    PropertyDef(name="id", type="str"),
                    PropertyDef(name="board_id", type="str", required=False),
                ],
                primary_key="id",
            ),
        ],
        link_types=[
            LinkTypeDef(
                api_name="onBoard",
                from_type="Card",
                to_type="Board",
                cardinality=Cardinality.MANY_TO_ONE,
                description="Card -> its board",
            ),
        ],
        functions=[
            FunctionDef(
                api_name="countCards",
                description="Count visible cards",
                input_description="none",
                output_description="int",
            ),
        ],
    )


def _kanban_policy(make_policy: PolicyFactory) -> ScopePolicy:
    return make_policy(
        levels=["board"],
        rules={
            "Board": [SelfScope(level="board")],
            "Card": [DirectProperty(level="board", property_name="board_id")],
        },
        min_n=1,
    )


def test_two_ontologies_coexist_without_cross_talk(
    make_registry: RegistryFactory,
    make_policy: PolicyFactory,
) -> None:
    library_client, _ = _build_client(_librarian(), make_registry, make_policy)

    kanban_registry = _kanban_registry(make_registry)

    def _count_cards(query: BoundQuery, params: dict[str, Any]) -> int:
        return len(query.list("Card", limit=None))

    kanban_functions = FunctionRegistry(kanban_registry)
    kanban_functions.register("countCards", _count_cards)
    kanban_ontology = OntologyDef(
        name="kanban",
        registry=kanban_registry,
        policy=_kanban_policy(make_policy),
        functions=kanban_functions,
    )
    kanban_ontology.validate()
    kanban_store = ObjectStore(kanban_registry)
    kanban_store.insert("Board", {"id": "board-1"}, SRC)
    kanban_store.insert("Card", {"id": "card-1", "board_id": "board-1"}, SRC)
    kanban_consumer = Consumer(
        actor_id="kb-1", role="Member", scope_level="board", scope_id="board-1", kind="human"
    )
    kanban_client = OntologyClient(kanban_ontology, kanban_store, kanban_consumer)

    # Neither ontology sees the other's types/functions/objects: a Board is
    # simply absent from the library store (no row of that object_type was
    # ever written there), and calling the other ontology's function name is
    # rejected as unregistered on THIS ontology's `FunctionRegistry`.
    assert library_client.get("Book", "book-1") is not None
    assert library_client.get("Board", "board-1") is None
    with raises_code(PreconditionFailed, "FUNCTION_ERROR"):
        library_client.call_function("countCards", {})

    assert kanban_client.call_function("countCards", {}) == 1
    assert kanban_client.get("Book", "book-1") is None
    with raises_code(PreconditionFailed, "FUNCTION_ERROR"):
        kanban_client.call_function("countBooksOnShelf", {"shelf_id": "shelf-1"})
