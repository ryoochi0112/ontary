"""Integration tests for `ontary.query.GuardedQuery` on a toy ontology.

The toy ontology is a small "library" domain (Library/Shelf/Book/Loan),
matching the style of `tests/test_scope.py` -- deliberately NOT named after
DSO. It is extended here with sensitivity-tagged properties (an
`ai_usable=False` shelf note, a `human_visible=False` book acquisition
cost) and an `identity_revealing` link (Loan -> Borrower) so redaction and
identity-revealing traversal denial can be exercised end to end.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest
from conftest import raises_code

from ontary.errors import InternalError, ValidationFailed, VisibilityError
from ontary.meta import (
    Cardinality,
    LinkTypeDef,
    ObjectTypeDef,
    OntologyRegistry,
    PropertyDef,
    Sensitivity,
)
from ontary.query import GuardedQuery
from ontary.scope import (
    DirectProperty,
    RowVisibilityStore,
    ScopePolicy,
    SelfScope,
    ViaLink,
)
from ontary.security import Consumer
from ontary.store import ObjectStore, Source, Store

LEVELS = ["shelf", "library"]
SRC = Source(source_system="test")


RegistryFactory = Callable[..., OntologyRegistry]
PolicyFactory = Callable[..., ScopePolicy]
StoreFactory = Callable[..., Store]


def _library_registry(make_registry: RegistryFactory) -> OntologyRegistry:
    registry = make_registry()
    registry.register_object_type(
        ObjectTypeDef(
            api_name="Library",
            display_name="Library",
            description="A library building",
            layer="L0",
            properties=[PropertyDef(name="id", type="str")],
            primary_key="id",
        )
    )
    registry.register_object_type(
        ObjectTypeDef(
            api_name="Shelf",
            display_name="Shelf",
            description="A shelf inside a library",
            layer="L0",
            properties=[
                PropertyDef(name="id", type="str"),
                PropertyDef(name="library_id", type="str", required=False),
                # ai_usable=False: internal shelf note, never surfaced to an
                # AI consumer.
                PropertyDef(
                    name="note",
                    type="str",
                    required=False,
                    sensitivity=Sensitivity(ai_usable=False, human_visible=True),
                ),
            ],
            primary_key="id",
        )
    )
    registry.register_object_type(
        ObjectTypeDef(
            api_name="Book",
            display_name="Book",
            description="A book, shelved somewhere",
            layer="L0",
            properties=[
                PropertyDef(name="id", type="str"),
                PropertyDef(name="shelf_id", type="str", required=False),
                PropertyDef(name="rating", type="float", required=False),
                # human_visible=False: acquisition cost, kept from human
                # readers but usable by an AI (e.g. an ordering assistant).
                PropertyDef(
                    name="acquisition_cost",
                    type="float",
                    required=False,
                    sensitivity=Sensitivity(ai_usable=True, human_visible=False),
                ),
                # ai_usable=False: an internal-only scoring signal, kept
                # from an AI consumer but visible to a human.
                PropertyDef(
                    name="internal_score",
                    type="float",
                    required=False,
                    sensitivity=Sensitivity(ai_usable=False, human_visible=True),
                ),
            ],
            primary_key="id",
        )
    )
    registry.register_object_type(
        ObjectTypeDef(
            api_name="Loan",
            display_name="Loan",
            description="A book on loan",
            layer="L0",
            properties=[PropertyDef(name="id", type="str")],
            primary_key="id",
        )
    )
    registry.register_object_type(
        ObjectTypeDef(
            api_name="Borrower",
            display_name="Borrower",
            description="The person a Loan is out to",
            layer="L0",
            properties=[PropertyDef(name="id", type="str")],
            primary_key="id",
        )
    )
    registry.register_object_type(
        ObjectTypeDef(
            api_name="Genre",
            display_name="Genre",
            description="An unscoped canonical taxonomy entry",
            layer="L0",
            properties=[PropertyDef(name="id", type="str")],
            primary_key="id",
        )
    )
    registry.register_link_type(
        LinkTypeDef(
            api_name="onShelf",
            from_type="Book",
            to_type="Shelf",
            cardinality=Cardinality.MANY_TO_ONE,
            description="Book -> its shelf",
        )
    )
    registry.register_link_type(
        LinkTypeDef(
            api_name="inLibrary",
            from_type="Shelf",
            to_type="Library",
            cardinality=Cardinality.MANY_TO_ONE,
            description="Shelf -> its library",
        )
    )
    registry.register_link_type(
        LinkTypeDef(
            api_name="loanedBook",
            from_type="Loan",
            to_type="Book",
            cardinality=Cardinality.MANY_TO_ONE,
            description="Loan -> the book on loan",
        )
    )
    registry.register_link_type(
        LinkTypeDef(
            api_name="loanedTo",
            from_type="Loan",
            to_type="Borrower",
            cardinality=Cardinality.MANY_TO_ONE,
            description="Loan -> borrower; traversing re-identifies them",
            identity_revealing=True,
        )
    )
    return registry


def _library_policy(make_policy: PolicyFactory) -> ScopePolicy:
    return make_policy(
        levels=LEVELS,
        unscoped_types={"Genre"},
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
                ViaLink(link_api_name="onShelf", direction="from", parent_type="Shelf"),
            ],
            "Loan": [
                ViaLink(link_api_name="loanedBook", direction="from", parent_type="Book"),
            ],
        },
        min_n=3,
    )

def _seed_two_shelves(store: Store) -> None:
    store.insert("Library", {"id": "lib-1"}, SRC)
    store.insert("Library", {"id": "lib-2"}, SRC)
    store.insert("Shelf", {"id": "shelf-1", "note": "fiction"}, SRC)
    store.insert("Shelf", {"id": "shelf-2", "note": "nonfiction"}, SRC)
    store.create_link("inLibrary", "shelf-1", "lib-1")
    store.create_link("inLibrary", "shelf-2", "lib-2")


def _human(scope_level: str, scope_id: str, actor_id: str = "u1") -> Consumer:
    return Consumer(
        actor_id=actor_id,
        role="Member",
        scope_level=scope_level,
        scope_id=scope_id,
        kind="human",
    )


def _ai(scope_level: str, scope_id: str, actor_id: str = "agent-1") -> Consumer:
    return Consumer(
        actor_id=actor_id,
        role="Agent",
        scope_level=scope_level,
        scope_id=scope_id,
        kind="ai",
    )


# -- visibility ---------------------------------------------------------


def test_visible_read_within_scope(
    make_registry: RegistryFactory,
    make_policy: PolicyFactory,
    make_store: StoreFactory,
) -> None:
    registry = _library_registry(make_registry)
    store = make_store(registry)
    _seed_two_shelves(store)
    store.insert("Book", {"id": "book-1", "shelf_id": "shelf-1"}, SRC)
    gq = GuardedQuery(store, registry, _library_policy(make_policy))

    consumer = _human("shelf", "shelf-1")
    result = gq.get_object(consumer, "Book", "book-1")
    assert result is not None
    assert result.payload["id"] == "book-1"


def test_visibility_denied_outside_scope(
    make_registry: RegistryFactory,
    make_policy: PolicyFactory,
    make_store: StoreFactory,
) -> None:
    registry = _library_registry(make_registry)
    store = make_store(registry)
    _seed_two_shelves(store)
    store.insert("Book", {"id": "book-1", "shelf_id": "shelf-1"}, SRC)
    gq = GuardedQuery(store, registry, _library_policy(make_policy))

    consumer = _human("shelf", "shelf-2")
    with raises_code(VisibilityError, "VISIBILITY_DENIED"):
        gq.get_object(consumer, "Book", "book-1")


def test_get_objects_filters_out_of_scope_rows(
    make_registry: RegistryFactory,
    make_policy: PolicyFactory,
    make_store: StoreFactory,
) -> None:
    registry = _library_registry(make_registry)
    store = make_store(registry)
    _seed_two_shelves(store)
    store.insert("Book", {"id": "book-1", "shelf_id": "shelf-1"}, SRC)
    store.insert("Book", {"id": "book-2", "shelf_id": "shelf-2"}, SRC)
    gq = GuardedQuery(store, registry, _library_policy(make_policy))

    consumer = _human("shelf", "shelf-1")
    results = gq.get_objects(consumer, "Book")
    assert [r.payload["id"] for r in results] == ["book-1"]


def test_unscoped_types_readable_by_anyone(
    make_registry: RegistryFactory,
    make_policy: PolicyFactory,
    make_store: StoreFactory,
) -> None:
    registry = _library_registry(make_registry)
    store = make_store(registry)
    store.insert("Genre", {"id": "scifi"}, SRC)
    gq = GuardedQuery(store, registry, _library_policy(make_policy))

    for consumer in (_human("shelf", "shelf-99"), _ai("library", "lib-99")):
        result = gq.get_object(consumer, "Genre", "scifi")
        assert result is not None
        assert result.payload["id"] == "scifi"


def test_deny_by_default_when_chain_broken(
    make_registry: RegistryFactory,
    make_policy: PolicyFactory,
    make_store: StoreFactory,
) -> None:
    registry = _library_registry(make_registry)
    store = make_store(registry)
    # No shelf_id, no onShelf link: chain is unresolvable.
    store.insert("Book", {"id": "book-orphan"}, SRC)
    gq = GuardedQuery(store, registry, _library_policy(make_policy))

    consumer = _human("shelf", "shelf-1")
    with raises_code(VisibilityError, "VISIBILITY_DENIED"):
        gq.get_object(consumer, "Book", "book-orphan")

    # get_objects silently filters it out rather than raising.
    assert gq.get_objects(consumer, "Book") == []


# -- redaction ------------------------------------------------------------


def test_ai_usable_false_hidden_from_ai_consumer(
    make_registry: RegistryFactory,
    make_policy: PolicyFactory,
    make_store: StoreFactory,
) -> None:
    registry = _library_registry(make_registry)
    store = make_store(registry)
    _seed_two_shelves(store)
    gq = GuardedQuery(store, registry, _library_policy(make_policy))

    human = _human("shelf", "shelf-1")
    ai = _ai("shelf", "shelf-1")

    human_view = gq.get_object(human, "Shelf", "shelf-1")
    ai_view = gq.get_object(ai, "Shelf", "shelf-1")
    assert human_view is not None and human_view.payload["note"] == "fiction"
    assert ai_view is not None and "note" not in ai_view.payload


def test_human_visible_false_hidden_from_human_consumer(
    make_registry: RegistryFactory,
    make_policy: PolicyFactory,
    make_store: StoreFactory,
) -> None:
    registry = _library_registry(make_registry)
    store = make_store(registry)
    _seed_two_shelves(store)
    store.insert(
        "Book",
        {"id": "book-1", "shelf_id": "shelf-1", "acquisition_cost": 42.0},
        SRC,
    )
    gq = GuardedQuery(store, registry, _library_policy(make_policy))

    human = _human("shelf", "shelf-1")
    ai = _ai("shelf", "shelf-1")

    human_view = gq.get_object(human, "Book", "book-1")
    ai_view = gq.get_object(ai, "Book", "book-1")
    assert human_view is not None and "acquisition_cost" not in human_view.payload
    assert ai_view is not None and ai_view.payload["acquisition_cost"] == 42.0


def test_scope_key_field_exempt_from_where_gate_but_still_redacted(
    make_registry: RegistryFactory,
    make_policy: PolicyFactory,
    make_store: StoreFactory,
) -> None:
    # shelf_id is a DirectProperty scope-routing field: a same-scope
    # consumer may filter by it even though it is not itself sensitivity-
    # tagged. (No hidden-field collision here, this documents that filtering
    # by a resolved-scope property that IS visible works normally.)
    registry = _library_registry(make_registry)
    store = make_store(registry)
    _seed_two_shelves(store)
    store.insert("Book", {"id": "book-1", "shelf_id": "shelf-1"}, SRC)
    gq = GuardedQuery(store, registry, _library_policy(make_policy))

    consumer = _human("shelf", "shelf-1")
    results = gq.get_objects(consumer, "Book", where={"shelf_id": "shelf-1"})
    assert [r.payload["id"] for r in results] == ["book-1"]


def test_where_on_hidden_field_denied_for_human_allowed_for_ai(
    make_registry: RegistryFactory,
    make_policy: PolicyFactory,
    make_store: StoreFactory,
) -> None:
    registry = _library_registry(make_registry)
    store = make_store(registry)
    _seed_two_shelves(store)
    store.insert(
        "Book",
        {"id": "book-1", "shelf_id": "shelf-1", "acquisition_cost": 42.0},
        SRC,
    )
    gq = GuardedQuery(store, registry, _library_policy(make_policy))

    human = _human("shelf", "shelf-1")
    ai = _ai("shelf", "shelf-1")

    with raises_code(VisibilityError, "VISIBILITY_DENIED"):
        gq.get_objects(human, "Book", where={"acquisition_cost": 42.0})

    # AI is permitted (acquisition_cost is ai_usable) -- no raise.
    results = gq.get_objects(ai, "Book", where={"acquisition_cost": 42.0})
    assert [r.payload["id"] for r in results] == ["book-1"]


def test_unknown_where_key_is_rejected_before_non_match(
    make_registry: RegistryFactory,
    make_policy: PolicyFactory,
    make_store: StoreFactory,
) -> None:
    registry = _library_registry(make_registry)
    store = make_store(registry)
    _seed_two_shelves(store)
    store.insert(
        "Book", {"id": "book-1", "shelf_id": "shelf-1", "rating": 4.0}, SRC
    )
    gq = GuardedQuery(store, registry, _library_policy(make_policy))

    with raises_code(ValidationFailed, "UNKNOWN_FIELD") as excinfo:
        gq.get_objects(
            _human("shelf", "shelf-1"), "Book", where={"ratng": 4.0}
        )
    assert str(excinfo.value) == "Book: where= names unknown field(s) ['ratng']"


# -- min-N ------------------------------------------------------------


def test_aggregate_below_min_n_raises(
    make_registry: RegistryFactory,
    make_policy: PolicyFactory,
    make_store: StoreFactory,
) -> None:
    registry = _library_registry(make_registry)
    store = make_store(registry)
    _seed_two_shelves(store)
    store.insert("Book", {"id": "book-1", "shelf_id": "shelf-1", "rating": 4.0}, SRC)
    store.insert("Book", {"id": "book-2", "shelf_id": "shelf-1", "rating": 5.0}, SRC)
    gq = GuardedQuery(store, registry, _library_policy(make_policy))

    consumer = _human("shelf", "shelf-1")
    with raises_code(VisibilityError, "MIN_N_VIOLATION") as excinfo:
        gq.aggregate(consumer, "Book", "rating")
    message = str(excinfo.value)
    assert "2" not in message
    assert "withheld" in message


def test_aggregate_at_min_n_passes(
    make_registry: RegistryFactory,
    make_policy: PolicyFactory,
    make_store: StoreFactory,
) -> None:
    registry = _library_registry(make_registry)
    store = make_store(registry)
    _seed_two_shelves(store)
    store.insert("Book", {"id": "book-1", "shelf_id": "shelf-1", "rating": 3.0}, SRC)
    store.insert("Book", {"id": "book-2", "shelf_id": "shelf-1", "rating": 4.0}, SRC)
    store.insert("Book", {"id": "book-3", "shelf_id": "shelf-1", "rating": 5.0}, SRC)
    gq = GuardedQuery(store, registry, _library_policy(make_policy))

    consumer = _human("shelf", "shelf-1")
    result = gq.aggregate(consumer, "Book", "rating")
    assert result == pytest.approx(4.0)


def test_aggregate_refuses_an_internal_non_float_result(
    make_registry: RegistryFactory,
    make_policy: PolicyFactory,
    make_store: StoreFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = _library_registry(make_registry)
    query = GuardedQuery(make_store(registry), registry, _library_policy(make_policy))
    monkeypatch.setattr(query, "_aggregate", lambda *args: {})

    with raises_code(InternalError, "INTERNAL_ERROR"):
        query.aggregate(_human("shelf", "shelf-1"), "Book", "rating")


def test_aggregate_by_refuses_an_internal_non_dict_result(
    make_registry: RegistryFactory,
    make_policy: PolicyFactory,
    make_store: StoreFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = _library_registry(make_registry)
    query = GuardedQuery(make_store(registry), registry, _library_policy(make_policy))
    monkeypatch.setattr(query, "_aggregate", lambda *args: 1.0)

    with raises_code(InternalError, "INTERNAL_ERROR"):
        query.aggregate_by(
            _human("shelf", "shelf-1"), "Book", "rating", "shelf_id"
        )


def test_aggregate_zero_rows_raises_min_n(
    make_registry: RegistryFactory,
    make_policy: PolicyFactory,
    make_store: StoreFactory,
) -> None:
    registry = _library_registry(make_registry)
    store = make_store(registry)
    _seed_two_shelves(store)
    gq = GuardedQuery(store, registry, _library_policy(make_policy))

    consumer = _human("shelf", "shelf-1")
    with raises_code(VisibilityError, "MIN_N_VIOLATION") as excinfo:
        gq.aggregate(consumer, "Book", "rating")
    message = str(excinfo.value)
    assert "0 contributors" not in message
    assert "withheld" in message


def test_grouped_aggregate_min_n_message_withholds_distinctive_count(
    make_registry: RegistryFactory,
    make_policy: PolicyFactory,
    make_store: StoreFactory,
) -> None:
    registry = _library_registry(make_registry)
    store = make_store(registry)
    _seed_two_shelves(store)
    for i in range(3):
        store.insert(
            "Book",
            {"id": f"book-{i}", "shelf_id": "shelf-1", "rating": 4.0},
            SRC,
        )
    policy = _library_policy(make_policy).model_copy(update={"min_n": 5})
    gq = GuardedQuery(store, registry, policy)

    with raises_code(VisibilityError, "MIN_N_VIOLATION") as excinfo:
        gq.aggregate_by(
            _human("shelf", "shelf-1"), "Book", "rating", "shelf_id"
        )

    message = str(excinfo.value)
    assert "3" not in message
    assert "withheld" in message


def test_aggregate_group_by_min_n_per_group(
    make_registry: RegistryFactory,
    make_policy: PolicyFactory,
    make_store: StoreFactory,
) -> None:
    registry = _library_registry(make_registry)
    store = make_store(registry)
    _seed_two_shelves(store)
    for i in range(3):
        store.insert(
            "Book",
            {"id": f"book-a{i}", "shelf_id": "shelf-1", "rating": 4.0},
            SRC,
        )
    store.insert("Book", {"id": "book-b0", "shelf_id": "shelf-2", "rating": 2.0}, SRC)
    gq = GuardedQuery(store, registry, _library_policy(make_policy))

    # company/library-scoped consumer sees both shelves.
    consumer = _human("library", "lib-1")
    # lib-1 owns shelf-1 only; shelf-2 belongs to lib-2, so aggregate_by()
    # over all Books group_by=shelf_id only ever sees shelf-1 rows for this
    # consumer -- below-min-N rows from a shelf outside scope never leak in
    # (nor would they trip a min-N visibility denial the consumer can't even see).
    result = gq.aggregate_by(consumer, "Book", "rating", "shelf_id")
    assert result == {"shelf-1": pytest.approx(4.0)}


def test_aggregate_by_empty_group_by_raises_coded_error_not_bare_assert(
    make_registry: RegistryFactory,
    make_policy: PolicyFactory,
    make_store: StoreFactory,
) -> None:
    """A falsy-but-non-`None` `group_by` (`""`) must never reach
    `_aggregate`'s `if group_by:` truthiness branch and silently collapse to
    the ungrouped path -- `aggregate_by` refuses it up front with a coded
    `INVALID_GROUP_BY` validation-kind error, not the `assert isinstance(result, dict)` that `python
    -O` strips (that assert would otherwise pass a bare `float` back to a
    caller typed as `dict[str, float]`, exploding far from this call)."""
    registry = _library_registry(make_registry)
    store = make_store(registry)
    _seed_two_shelves(store)
    store.insert("Book", {"id": "book-1", "shelf_id": "shelf-1", "rating": 4.0}, SRC)
    gq = GuardedQuery(store, registry, _library_policy(make_policy))

    consumer = _human("shelf", "shelf-1")
    with raises_code(ValidationFailed, "INVALID_GROUP_BY"):
        gq.aggregate_by(consumer, "Book", "rating", "")


# -- identity-revealing traversal ------------------------------------------


def test_identity_revealing_traverse_denied_for_human_allowed_for_ai(
    make_registry: RegistryFactory,
    make_policy: PolicyFactory,
    make_store: StoreFactory,
) -> None:
    registry = _library_registry(make_registry)
    store = make_store(registry)
    _seed_two_shelves(store)
    store.insert("Book", {"id": "book-1", "shelf_id": "shelf-1"}, SRC)
    store.insert("Loan", {"id": "loan-1"}, SRC)
    store.insert("Borrower", {"id": "borrower-1"}, SRC)
    store.create_link("loanedBook", "loan-1", "book-1")
    store.create_link("loanedTo", "loan-1", "borrower-1")
    gq = GuardedQuery(store, registry, _library_policy(make_policy))

    human = _human("shelf", "shelf-1")
    ai = _ai("shelf", "shelf-1")

    with raises_code(VisibilityError, "VISIBILITY_DENIED"):
        gq.traverse(human, "loanedTo", "loan-1")

    # Borrower has no scope rule declared -> its chain is unresolvable ->
    # deny-by-default even for the AI consumer that is allowed to traverse
    # the link itself. Give Borrower an unscoped-type escape hatch instead
    # to prove the AI traversal itself is not blocked by identity_revealing.
    policy = _library_policy(make_policy)
    policy.unscoped_types = policy.unscoped_types | {"Borrower"}
    gq2 = GuardedQuery(store, registry, policy)
    results = gq2.traverse(ai, "loanedTo", "loan-1")
    assert [r.payload["id"] for r in results] == ["borrower-1"]


def test_traverse_returns_only_visible_targets(
    make_registry: RegistryFactory,
    make_policy: PolicyFactory,
    make_store: StoreFactory,
) -> None:
    registry = _library_registry(make_registry)
    store = make_store(registry)
    _seed_two_shelves(store)
    store.insert("Book", {"id": "book-1", "shelf_id": "shelf-1"}, SRC)
    store.insert("Book", {"id": "book-2", "shelf_id": "shelf-2"}, SRC)
    store.create_link("onShelf", "book-1", "shelf-1")
    gq = GuardedQuery(store, registry, _library_policy(make_policy))

    consumer = _human("shelf", "shelf-1")
    # inLibrary runs Shelf -> Library, not Book -> Shelf; use onShelf here.
    results = gq.traverse(consumer, "onShelf", "book-1")
    assert [r.payload["id"] for r in results] == ["shelf-1"]

    other_consumer = _human("shelf", "shelf-2")
    assert gq.traverse(other_consumer, "onShelf", "book-1") == []


# -- AC7: no public read path bypasses guards ------------------------------


def test_object_store_exposes_no_public_read_method() -> None:
    """AC7's invariant is "no read path bypasses guards for a CONSUMER" --
    not "ObjectStore has zero public methods". `read_current`/`read_all`
    are a deliberate, documented exception (finding 5): a trusted raw-read
    seam for action-handler bodies, which already run inside
    `ActionExecutor.execute`'s audited, permission/scope-checked pipeline,
    and other authoring/loader code -- never for a CONSUMER (human/AI
    client, MCP tool, Function). `GuardedQuery` remains the only read path
    a consumer may reach.
    """
    public_read_names = {"get", "get_all", "get_as_of", "list", "query"}
    store_public_attrs = {
        name for name in dir(ObjectStore) if not name.startswith("_")
    }
    assert public_read_names.isdisjoint(store_public_attrs)
    assert "read_current" in store_public_attrs
    assert "read_all" in store_public_attrs


def test_guarded_query_filters_what_store_get_all_returns(
    make_registry: RegistryFactory,
    make_policy: PolicyFactory,
    make_store: StoreFactory,
) -> None:
    registry = _library_registry(make_registry)
    store = make_store(registry)
    _seed_two_shelves(store)
    store.insert("Book", {"id": "book-1", "shelf_id": "shelf-1"}, SRC)
    store.insert("Book", {"id": "book-2", "shelf_id": "shelf-2"}, SRC)
    gq = GuardedQuery(store, registry, _library_policy(make_policy))

    # The raw store read (package-private, never exposed publicly) returns
    # every row regardless of scope.
    raw_rows = store.read_all("Book")
    assert {r.payload["id"] for r in raw_rows} == {"book-1", "book-2"}

    # GuardedQuery -- the only public read path -- filters that same raw
    # data down to what the consumer's scope actually covers.
    consumer = _human("shelf", "shelf-1")
    guarded_rows = gq.get_objects(consumer, "Book")
    assert {r.payload["id"] for r in guarded_rows} == {"book-1"}


# -- scope-key exemption is per-type, not pooled globally (reviewer P1:
# query.py:80-95 + 145-155) -------------------------------------------------


def _shared_field_registry(make_registry: RegistryFactory) -> OntologyRegistry:
    """Two unrelated types (A, B) that both happen to declare a property
    named "person_id" -- but only A's rule exempts it as a scope-routing
    key. B's "person_id" is a sensitivity-hidden identity field that must
    NOT be exempted just because A declared a same-named DirectProperty."""
    registry = make_registry()
    registry.register_object_type(
        ObjectTypeDef(
            api_name="TeamA",
            display_name="TeamA",
            description="Scope-routing type A: person_id is its own scope key",
            layer="L0",
            properties=[
                PropertyDef(name="id", type="str"),
                PropertyDef(name="person_id", type="str", required=False),
            ],
            primary_key="id",
        )
    )
    registry.register_object_type(
        ObjectTypeDef(
            api_name="RecordB",
            display_name="RecordB",
            description=(
                "Unrelated type B: person_id here is a hidden identity field, "
                "not a scope-routing key"
            ),
            layer="L0",
            properties=[
                PropertyDef(name="id", type="str"),
                PropertyDef(name="group_id", type="str", required=False),
                PropertyDef(
                    name="person_id",
                    type="str",
                    required=False,
                    sensitivity=Sensitivity(ai_usable=True, human_visible=False),
                ),
                PropertyDef(name="score", type="float", required=False),
            ],
            primary_key="id",
        )
    )
    return registry


def _shared_field_policy(make_policy: PolicyFactory) -> ScopePolicy:
    return make_policy(
        levels=["person", "group"],
        rules={
            "TeamA": [DirectProperty(level="person", property_name="person_id")],
            "RecordB": [DirectProperty(level="group", property_name="group_id")],
        },
        min_n=3,
    )


def test_scope_key_exemption_does_not_pool_across_types(
    make_registry: RegistryFactory,
    make_policy: PolicyFactory,
    make_store: StoreFactory,
) -> None:
    registry = _shared_field_registry(make_registry)
    store = make_store(registry)
    store.insert("TeamA", {"id": "a-1", "person_id": "p-1"}, SRC)
    store.insert(
        "RecordB", {"id": "b-1", "group_id": "p-1", "person_id": "p-1"}, SRC
    )
    gq = GuardedQuery(store, registry, _shared_field_policy(make_policy))

    consumer = _human("group", "p-1")

    # (a) RecordB.person_id is hidden from a human (human_visible=False) and
    # is NOT declared as a DirectProperty on RecordB itself, only on the
    # unrelated TeamA -- the gate must still apply here.
    with raises_code(VisibilityError, "VISIBILITY_DENIED"):
        gq.get_objects(consumer, "RecordB", where={"person_id": "p-1"})

    # (b) TeamA's OWN DirectProperty("person_id") exemption still works --
    # a person-scoped consumer may filter TeamA by its own scope key.
    person_consumer = _human("person", "p-1")
    results = gq.get_objects(person_consumer, "TeamA", where={"person_id": "p-1"})
    assert [r.payload["id"] for r in results] == ["a-1"]


# -- min-N visibility denial must not echo a sensitivity-hidden group key value


def test_min_n_violation_message_does_not_leak_hidden_group_key_value(
    make_registry: RegistryFactory,
    make_policy: PolicyFactory,
    make_store: StoreFactory,
) -> None:
    registry = _shared_field_registry(make_registry)
    store = make_store(registry)
    # group_id doubles as RecordB's scope-routing DirectProperty AND (in
    # this variant) a sensitivity-hidden field, so it is exempt from the
    # where=/group_by= gate but must still never appear as a raw value in
    # a min-N visibility-denial message.
    obj_def = registry.get_object_type("RecordB")
    for prop in obj_def.properties:
        if prop.name == "group_id":
            prop.sensitivity = Sensitivity(ai_usable=True, human_visible=False)

    store.insert(
        "RecordB", {"id": "b-1", "group_id": "secret-group", "score": 1.0}, SRC
    )
    gq = GuardedQuery(store, registry, _shared_field_policy(make_policy))

    consumer = _human("group", "secret-group")
    with raises_code(VisibilityError, "MIN_N_VIOLATION") as excinfo:
        gq.aggregate_by(consumer, "RecordB", "score", "group_id")
    assert "secret-group" not in str(excinfo.value)


# -- non-"id" primary key (reviewer P1: query.py:118) -----------------------


def _code_pk_registry(make_registry: RegistryFactory) -> OntologyRegistry:
    """A toy type whose primary key is named "code", not "id" -- proves
    `GuardedQuery` never hardcodes the pk property name."""
    registry = make_registry()
    registry.register_object_type(
        ObjectTypeDef(
            api_name="Region",
            display_name="Region",
            description="A top-level scope with a non-'id' primary key",
            layer="L0",
            properties=[PropertyDef(name="code", type="str")],
            primary_key="code",
        )
    )
    registry.register_object_type(
        ObjectTypeDef(
            api_name="Item",
            display_name="Item",
            description="An item scoped to a Region, also keyed by 'code'",
            layer="L0",
            properties=[
                PropertyDef(name="code", type="str"),
                PropertyDef(name="region_code", type="str", required=False),
                PropertyDef(name="rating", type="float", required=False),
            ],
            primary_key="code",
        )
    )
    registry.register_link_type(
        LinkTypeDef(
            api_name="itemInRegion",
            from_type="Item",
            to_type="Region",
            cardinality=Cardinality.MANY_TO_ONE,
            description="Item -> its region",
        )
    )
    return registry


def _code_pk_policy(make_policy: PolicyFactory) -> ScopePolicy:
    return make_policy(
        levels=["region"],
        rules={
            "Region": [SelfScope(level="region")],
            "Item": [DirectProperty(level="region", property_name="region_code")],
        },
        min_n=3,
    )


def test_non_id_primary_key_works_across_all_read_paths(
    make_registry: RegistryFactory,
    make_policy: PolicyFactory,
    make_store: StoreFactory,
) -> None:
    registry = _code_pk_registry(make_registry)
    store = make_store(registry)
    store.insert("Region", {"code": "reg-1"}, SRC)
    store.insert("Region", {"code": "reg-2"}, SRC)
    store.insert("Item", {"code": "item-1", "region_code": "reg-1"}, SRC)
    store.insert("Item", {"code": "item-2", "region_code": "reg-2"}, SRC)
    store.create_link("itemInRegion", "item-1", "reg-1")
    gq = GuardedQuery(store, registry, _code_pk_policy(make_policy))

    consumer = _human("region", "reg-1")

    # get_object
    result = gq.get_object(consumer, "Item", "item-1")
    assert result is not None and result.payload["code"] == "item-1"
    with raises_code(VisibilityError, "VISIBILITY_DENIED"):
        gq.get_object(consumer, "Item", "item-2")

    # get_objects
    assert [r.payload["code"] for r in gq.get_objects(consumer, "Item")] == ["item-1"]

    # traverse
    assert [r.payload["code"] for r in gq.traverse(consumer, "itemInRegion", "item-1")] == [
        "reg-1"
    ]

    # aggregate
    store.insert("Item", {"code": "item-3", "region_code": "reg-1", "rating": 3.0}, SRC)
    store.insert("Item", {"code": "item-4", "region_code": "reg-1", "rating": 4.0}, SRC)
    store.insert("Item", {"code": "item-5", "region_code": "reg-1", "rating": 5.0}, SRC)
    result = gq.aggregate(consumer, "Item", "rating")
    assert result == pytest.approx(4.0)


# -- min-N CONTRIBUTOR resolution (T4 review debt, closed in T8) ------------
#
# Extends the toy Library ontology with a Reader/Reading pair -- a Reading is
# a review submitted by a Reader, structurally mirroring DSO's
# Response/byPerson/Person shape -- to prove `ScopePolicy.contributor_rules`
# + `GuardedQuery.aggregate` de-duplicate contributors through the GENERIC
# public aggregate path (not a domain-private wrapper).


def _contributor_registry(make_registry: RegistryFactory) -> OntologyRegistry:
    registry = _library_registry(make_registry)
    registry.register_object_type(
        ObjectTypeDef(
            api_name="Reader",
            display_name="Reader",
            description="A person who submits Readings",
            layer="L0",
            properties=[PropertyDef(name="id", type="str")],
            primary_key="id",
        )
    )
    registry.register_object_type(
        ObjectTypeDef(
            api_name="Reading",
            display_name="Reading",
            description="A review of a Book submitted by a Reader",
            layer="L0",
            properties=[
                PropertyDef(name="id", type="str"),
                PropertyDef(name="shelf_id", type="str", required=False),
                PropertyDef(name="score", type="float", required=False),
                # human_visible=False, but Reading declares
                # `contributor_rules` (below) -- distinct-Reader min-N
                # de-dup is the ontology author's own asserted guarantee
                # that aggregating this otherwise-hidden field is safe (see
                # `GuardedQuery.aggregate`'s `contributor_dedup_declared`
                # exemption).
                PropertyDef(
                    name="raw_score",
                    type="float",
                    required=False,
                    sensitivity=Sensitivity(ai_usable=True, human_visible=False),
                ),
            ],
            primary_key="id",
        )
    )
    registry.register_link_type(
        LinkTypeDef(
            api_name="byReader",
            from_type="Reading",
            to_type="Reader",
            cardinality=Cardinality.MANY_TO_ONE,
            description="Reading -> the Reader who submitted it",
        )
    )
    return registry


def _contributor_policy(make_policy: PolicyFactory) -> ScopePolicy:
    policy = _library_policy(make_policy)
    policy.rules = {
        **policy.rules,
        "Reading": [
            DirectProperty(level="shelf", property_name="shelf_id"),
        ],
    }
    policy.contributor_rules = {
        "Reading": [ViaLink(link_api_name="byReader", direction="from", parent_type="Reader")],
        "Reader": [SelfScope(level="_contributor")],
    }
    return policy


def test_aggregate_min_n_contributor_dedup_below_threshold_raises(
    make_registry: RegistryFactory,
    make_policy: PolicyFactory,
    make_store: StoreFactory,
) -> None:
    """3 Readings from only 2 distinct Readers (one Reader submitted twice)
    must raise a min-N visibility denial even though there are 3 ROWS -- the row count
    alone would pass min_n=3, proving contributor de-dup (not row count) is
    what the generic aggregate path enforces once contributor rules are
    declared."""
    registry = _contributor_registry(make_registry)
    store = make_store(registry)
    _seed_two_shelves(store)
    store.insert("Reader", {"id": "reader-1"}, SRC)
    store.insert("Reader", {"id": "reader-2"}, SRC)
    store.insert("Reading", {"id": "r1", "shelf_id": "shelf-1", "score": 4.0}, SRC)
    store.insert("Reading", {"id": "r2", "shelf_id": "shelf-1", "score": 5.0}, SRC)
    store.insert("Reading", {"id": "r3", "shelf_id": "shelf-1", "score": 3.0}, SRC)
    store.create_link("byReader", "r1", "reader-1")
    store.create_link("byReader", "r2", "reader-1")  # same reader as r1
    store.create_link("byReader", "r3", "reader-2")
    gq = GuardedQuery(store, registry, _contributor_policy(make_policy))

    consumer = _human("shelf", "shelf-1")
    with raises_code(VisibilityError, "MIN_N_VIOLATION"):
        gq.aggregate(consumer, "Reading", "score")


def test_aggregate_min_n_contributor_dedup_at_threshold_passes(
    make_registry: RegistryFactory,
    make_policy: PolicyFactory,
    make_store: StoreFactory,
) -> None:
    """3 Readings from 3 distinct Readers passes min_n=3."""
    registry = _contributor_registry(make_registry)
    store = make_store(registry)
    _seed_two_shelves(store)
    for i in range(3):
        store.insert("Reader", {"id": f"reader-{i}"}, SRC)
        store.insert(
            "Reading", {"id": f"r{i}", "shelf_id": "shelf-1", "score": 4.0}, SRC
        )
        store.create_link("byReader", f"r{i}", f"reader-{i}")
    gq = GuardedQuery(store, registry, _contributor_policy(make_policy))

    consumer = _human("shelf", "shelf-1")
    result = gq.aggregate(consumer, "Reading", "score")
    assert result == pytest.approx(4.0)


def test_aggregate_without_contributor_rules_still_counts_rows(
    make_registry: RegistryFactory,
    make_policy: PolicyFactory,
    make_store: StoreFactory,
) -> None:
    """A type with NO contributor_rules declared (e.g. Book) keeps the
    row-count floor unchanged -- the T8 contributor hook is additive, never
    a behavior change for ontologies that don't declare it."""
    registry = _contributor_registry(make_registry)
    store = make_store(registry)
    _seed_two_shelves(store)
    store.insert("Book", {"id": "book-1", "shelf_id": "shelf-1", "rating": 3.0}, SRC)
    store.insert("Book", {"id": "book-2", "shelf_id": "shelf-1", "rating": 4.0}, SRC)
    store.insert("Book", {"id": "book-3", "shelf_id": "shelf-1", "rating": 5.0}, SRC)
    gq = GuardedQuery(store, registry, _contributor_policy(make_policy))

    consumer = _human("shelf", "shelf-1")
    result = gq.aggregate(consumer, "Book", "rating")
    assert result == pytest.approx(4.0)


def test_aggregate_contributor_unresolved_row_falls_back_to_row_count(
    make_registry: RegistryFactory,
    make_policy: PolicyFactory,
    make_store: StoreFactory,
) -> None:
    """A Reading with no byReader link at all (contributor unresolved) still
    counts toward the floor as its own row, mirroring the prototype's
    `_contributor_count` fallback -- it is never silently dropped."""
    registry = _contributor_registry(make_registry)
    store = make_store(registry)
    _seed_two_shelves(store)
    store.insert("Reader", {"id": "reader-1"}, SRC)
    store.insert("Reading", {"id": "r1", "shelf_id": "shelf-1", "score": 4.0}, SRC)
    store.create_link("byReader", "r1", "reader-1")
    # r2, r3 have no byReader link: unresolved contributors, each counts as
    # its own row.
    store.insert("Reading", {"id": "r2", "shelf_id": "shelf-1", "score": 5.0}, SRC)
    store.insert("Reading", {"id": "r3", "shelf_id": "shelf-1", "score": 3.0}, SRC)
    gq = GuardedQuery(store, registry, _contributor_policy(make_policy))

    consumer = _human("shelf", "shelf-1")
    result = gq.aggregate(consumer, "Reading", "score")
    assert result == pytest.approx(4.0)


def test_scope_policy_validate_covers_contributor_rules(
    make_registry: RegistryFactory,
    make_policy: PolicyFactory,
) -> None:
    """`ScopePolicy.validate()` catches undeclared refs in
    `contributor_rules` the same way it does for `rules`."""
    registry = _contributor_registry(make_registry)
    policy = _contributor_policy(make_policy)
    policy.contributor_rules["Reading"] = [
        ViaLink(link_api_name="nope", direction="from", parent_type="Reader")
    ]
    with raises_code(ValidationFailed, "SCOPE_POLICY_ERROR"):
        policy.validate(registry)


# -- two ontologies coexisting in one process -------------------------------


def test_two_ontologies_coexist_without_cross_talk(
    make_registry: RegistryFactory,
    make_policy: PolicyFactory,
    make_store: StoreFactory,
) -> None:
    registry_a = _library_registry(make_registry)
    store_a = make_store(registry_a)
    policy_a = _library_policy(make_policy)
    _seed_two_shelves(store_a)
    store_a.insert("Book", {"id": "book-1", "shelf_id": "shelf-1"}, SRC)
    gq_a = GuardedQuery(store_a, registry_a, policy_a)

    # A second, independent registry/policy/store -- same shape, disjoint
    # ids -- proves no module-global state leaks between ontology instances.
    registry_b = _library_registry(make_registry)
    store_b = make_store(registry_b)
    policy_b = _library_policy(make_policy)
    _seed_two_shelves(store_b)
    store_b.insert("Book", {"id": "book-9", "shelf_id": "shelf-1"}, SRC)
    gq_b = GuardedQuery(store_b, registry_b, policy_b)

    consumer = _human("shelf", "shelf-1")
    assert [r.payload["id"] for r in gq_a.get_objects(consumer, "Book")] == ["book-1"]
    assert [r.payload["id"] for r in gq_b.get_objects(consumer, "Book")] == ["book-9"]

    # Writing/reading through ontology A never surfaces in B's store.
    assert gq_b.get_object(consumer, "Book", "book-1") is None
    assert gq_a.get_object(consumer, "Book", "book-9") is None


# -- ScopePolicy.row_visibility (whole-branch review finding 1, AC10) -------
#
# `_hide_loaned_books_from_humans` mirrors the shape of a production
# row-visibility predicate on this toy ontology: a human never sees a Book
# that has an active Loan (probed via the narrow
# `RowVisibilityStore`, never the raw `ObjectStore`); an AI sees every Book
# regardless. Declared on top of -- never instead of -- the ordinary scope
# check every read path already runs.


def _hide_loaned_books_from_humans(
    row_store: RowVisibilityStore, consumer: Consumer, obj_type: str, row: dict
) -> bool:
    if consumer.kind != "human":
        return True
    loans = row_store.links_to("loanedBook", str(row["id"]))
    return not loans


def _row_visibility_policy(make_policy: PolicyFactory) -> ScopePolicy:
    policy = _library_policy(make_policy)
    policy.row_visibility = {"Book": _hide_loaned_books_from_humans}
    return policy


def _seed_loaned_and_unloaned_books(store: Store) -> None:
    _seed_two_shelves(store)
    store.insert("Book", {"id": "book-free", "shelf_id": "shelf-1", "rating": 4.0}, SRC)
    store.insert("Book", {"id": "book-loaned", "shelf_id": "shelf-1", "rating": 5.0}, SRC)
    store.insert("Loan", {"id": "loan-1"}, SRC)
    store.create_link("loanedBook", "loan-1", "book-loaned")


def test_row_visibility_hides_row_from_human_on_get_object(
    make_registry: RegistryFactory,
    make_policy: PolicyFactory,
    make_store: StoreFactory,
) -> None:
    registry = _library_registry(make_registry)
    store = make_store(registry)
    _seed_loaned_and_unloaned_books(store)
    gq = GuardedQuery(store, registry, _row_visibility_policy(make_policy))

    human = _human("shelf", "shelf-1")
    ai = _ai("shelf", "shelf-1")

    with raises_code(VisibilityError, "VISIBILITY_DENIED"):
        gq.get_object(human, "Book", "book-loaned")
    assert gq.get_object(human, "Book", "book-free") is not None

    assert gq.get_object(ai, "Book", "book-loaned") is not None


def test_row_visibility_filters_get_objects(
    make_registry: RegistryFactory,
    make_policy: PolicyFactory,
    make_store: StoreFactory,
) -> None:
    registry = _library_registry(make_registry)
    store = make_store(registry)
    _seed_loaned_and_unloaned_books(store)
    gq = GuardedQuery(store, registry, _row_visibility_policy(make_policy))

    human = _human("shelf", "shelf-1")
    ai = _ai("shelf", "shelf-1")

    assert {r.payload["id"] for r in gq.get_objects(human, "Book")} == {"book-free"}
    assert {r.payload["id"] for r in gq.get_objects(ai, "Book")} == {"book-free", "book-loaned"}


def test_row_visibility_filters_traverse(
    make_registry: RegistryFactory,
    make_policy: PolicyFactory,
    make_store: StoreFactory,
) -> None:
    registry = _library_registry(make_registry)
    store = make_store(registry)
    _seed_loaned_and_unloaned_books(store)
    gq = GuardedQuery(store, registry, _row_visibility_policy(make_policy))

    human = _human("shelf", "shelf-1")
    ai = _ai("shelf", "shelf-1")

    assert gq.traverse(human, "loanedBook", "loan-1") == []
    assert [r.payload["id"] for r in gq.traverse(ai, "loanedBook", "loan-1")] == ["book-loaned"]


def test_row_visibility_excludes_rows_before_min_n_counting_in_aggregate(
    make_registry: RegistryFactory,
    make_policy: PolicyFactory,
    make_store: StoreFactory,
) -> None:
    registry = _library_registry(make_registry)
    store = make_store(registry)
    _seed_two_shelves(store)
    # 3 rows total, but 2 are loaned -- a human only ever counts the 1
    # remaining visible row (below min_n=3), while an AI counts all 3.
    store.insert("Book", {"id": "book-free", "shelf_id": "shelf-1", "rating": 4.0}, SRC)
    store.insert("Book", {"id": "book-loaned-1", "shelf_id": "shelf-1", "rating": 5.0}, SRC)
    store.insert("Book", {"id": "book-loaned-2", "shelf_id": "shelf-1", "rating": 3.0}, SRC)
    store.insert("Loan", {"id": "loan-1"}, SRC)
    store.insert("Loan", {"id": "loan-2"}, SRC)
    store.create_link("loanedBook", "loan-1", "book-loaned-1")
    store.create_link("loanedBook", "loan-2", "book-loaned-2")
    gq = GuardedQuery(store, registry, _row_visibility_policy(make_policy))

    human = _human("shelf", "shelf-1")
    ai = _ai("shelf", "shelf-1")

    with raises_code(VisibilityError, "MIN_N_VIOLATION"):
        gq.aggregate(human, "Book", "rating")

    result = gq.aggregate(ai, "Book", "rating")
    assert result == pytest.approx(4.0)


def test_row_visibility_unaffected_when_no_predicate_declared(
    make_registry: RegistryFactory,
    make_policy: PolicyFactory,
    make_store: StoreFactory,
) -> None:
    """A type with no `row_visibility` entry is scope-only, exactly as
    before this task -- `_library_policy()` (used by every other test in this file)
    never declares one."""
    registry = _library_registry(make_registry)
    store = make_store(registry)
    _seed_loaned_and_unloaned_books(store)
    gq = GuardedQuery(
        store, registry, _library_policy(make_policy)
    )  # no row_visibility here

    human = _human("shelf", "shelf-1")
    assert gq.get_object(human, "Book", "book-loaned") is not None
    assert {r.payload["id"] for r in gq.get_objects(human, "Book")} == {
        "book-free",
        "book-loaned",
    }


def test_scope_policy_validate_rejects_row_visibility_on_unregistered_type(
    make_registry: RegistryFactory,
    make_policy: PolicyFactory,
) -> None:
    registry = _library_registry(make_registry)
    policy = _library_policy(make_policy)
    policy.row_visibility = {"NotARealType": _hide_loaned_books_from_humans}
    with raises_code(ValidationFailed, "SCOPE_POLICY_ERROR"):
        policy.validate(registry)


# -- aggregate value_field gating (whole-branch review finding 3, §7) -------


def test_aggregate_ai_usable_false_value_field_denied_for_ai(
    make_registry: RegistryFactory,
    make_policy: PolicyFactory,
    make_store: StoreFactory,
) -> None:
    registry = _library_registry(make_registry)
    store = make_store(registry)
    _seed_two_shelves(store)
    for i in range(3):
        store.insert(
            "Book",
            {"id": f"book-{i}", "shelf_id": "shelf-1", "internal_score": 4.0},
            SRC,
        )
    gq = GuardedQuery(store, registry, _library_policy(make_policy))

    ai = _ai("shelf", "shelf-1")
    with raises_code(VisibilityError, "VISIBILITY_DENIED"):
        gq.aggregate(ai, "Book", "internal_score")

    # A human (internal_score is human_visible) is unaffected.
    human = _human("shelf", "shelf-1")
    assert gq.aggregate(human, "Book", "internal_score") == pytest.approx(4.0)


def test_aggregate_human_visible_false_value_field_denied_for_human(
    make_registry: RegistryFactory,
    make_policy: PolicyFactory,
    make_store: StoreFactory,
) -> None:
    """`Book` declares no `contributor_rules` -- aggregating its hidden
    `acquisition_cost` must be denied for a human (no min-N-dedup trust
    signal exempts it)."""
    registry = _library_registry(make_registry)
    store = make_store(registry)
    _seed_two_shelves(store)
    for i in range(3):
        store.insert(
            "Book",
            {"id": f"book-{i}", "shelf_id": "shelf-1", "acquisition_cost": 10.0},
            SRC,
        )
    gq = GuardedQuery(store, registry, _library_policy(make_policy))

    human = _human("shelf", "shelf-1")
    with raises_code(VisibilityError, "VISIBILITY_DENIED"):
        gq.aggregate(human, "Book", "acquisition_cost")

    # An AI (acquisition_cost is ai_usable) is unaffected.
    ai = _ai("shelf", "shelf-1")
    assert gq.aggregate(ai, "Book", "acquisition_cost") == pytest.approx(10.0)


def test_aggregate_visible_field_still_works(
    make_registry: RegistryFactory,
    make_policy: PolicyFactory,
    make_store: StoreFactory,
) -> None:
    registry = _library_registry(make_registry)
    store = make_store(registry)
    _seed_two_shelves(store)
    for i in range(3):
        store.insert("Book", {"id": f"book-{i}", "shelf_id": "shelf-1", "rating": 4.0}, SRC)
    gq = GuardedQuery(store, registry, _library_policy(make_policy))

    human = _human("shelf", "shelf-1")
    ai = _ai("shelf", "shelf-1")
    assert gq.aggregate(human, "Book", "rating") == pytest.approx(4.0)
    assert gq.aggregate(ai, "Book", "rating") == pytest.approx(4.0)


def test_aggregate_non_numeric_declared_type_raises_coded_error(
    make_registry: RegistryFactory,
    make_policy: PolicyFactory,
    make_store: StoreFactory,
) -> None:
    """`aggregate` checks `value_field`'s DECLARED `PropertyType` before
    iterating any row: `Book.id` is declared `str`, so aggregating it must
    raise a validation-kind error (`NON_NUMERIC_AGGREGATE`), never attempt a
    `float()` coercion (m35-sdk-refactor T6/AC7)."""
    registry = _library_registry(make_registry)
    store = make_store(registry)
    _seed_two_shelves(store)
    store.insert("Book", {"id": "book-1", "shelf_id": "shelf-1"}, SRC)
    gq = GuardedQuery(store, registry, _library_policy(make_policy))

    human = _human("shelf", "shelf-1")
    with raises_code(ValidationFailed, "NON_NUMERIC_AGGREGATE"):
        gq.aggregate(human, "Book", "id")


def test_aggregate_bool_declared_type_raises_coded_error(
    make_policy: PolicyFactory,
    make_store: StoreFactory,
) -> None:
    """README:283 and the `NON_NUMERIC_AGGREGATE` contract both promise a
    `bool`-declared property is rejected too, not just `str` -- `bool` is
    excluded from `_NUMERIC_PROPERTY_TYPES` even though Python's `bool` is a
    subclass of `int` (m35-sdk-refactor T6)."""
    registry = OntologyRegistry()
    registry.register_object_type(
        ObjectTypeDef(
            api_name="Flag",
            display_name="Flag",
            description="A type with a bool-declared property",
            layer="L0",
            properties=[
                PropertyDef(name="id", type="str"),
                PropertyDef(name="active", type="bool", required=False),
            ],
            primary_key="id",
        )
    )
    store = make_store(registry)
    store.insert("Flag", {"id": "flag-1", "active": True}, SRC)
    gq = GuardedQuery(store, registry, _library_policy(make_policy))

    human = _human("shelf", "shelf-1")
    with raises_code(ValidationFailed, "NON_NUMERIC_AGGREGATE"):
        gq.aggregate(human, "Flag", "active")


def test_aggregate_value_field_gate_ignores_scope_key_exemption(
    make_registry: RegistryFactory,
    make_policy: PolicyFactory,
    make_store: StoreFactory,
) -> None:
    """`RecordB.group_id` doubles (in this variant) as BOTH a scope-routing
    `DirectProperty` key -- exempt from the where=/group_by= filter gate --
    AND a sensitivity-hidden field. `aggregate`'s value_field gate must use
    the un-exempted `_hidden_fields` set, NOT `_filter_hidden_fields`: a
    consumer may still FILTER by its own scope key, but aggregating BY that
    same hidden field is still denied -- the two gates are deliberately
    asymmetric (see `GuardedQuery.aggregate`'s docstring comment)."""
    registry = _shared_field_registry(make_registry)
    store = make_store(registry)
    obj_def = registry.get_object_type("RecordB")
    for prop in obj_def.properties:
        if prop.name == "group_id":
            prop.sensitivity = Sensitivity(ai_usable=True, human_visible=False)
    store.insert("RecordB", {"id": "b-1", "group_id": "secret-group"}, SRC)
    gq = GuardedQuery(store, registry, _shared_field_policy(make_policy))

    consumer = _human("group", "secret-group")

    # where= is still exempt (scope-routing DirectProperty key).
    results = gq.get_objects(consumer, "RecordB", where={"group_id": "secret-group"})
    assert [r.payload["id"] for r in results] == ["b-1"]

    # value_field is NOT exempt: still denied.
    with raises_code(VisibilityError, "VISIBILITY_DENIED"):
        gq.aggregate(consumer, "RecordB", "group_id")


def test_aggregate_hidden_field_exempt_when_contributor_dedup_declared(
    make_registry: RegistryFactory,
    make_policy: PolicyFactory,
    make_store: StoreFactory,
) -> None:
    """`Reading` DOES declare `contributor_rules` -- aggregating its hidden
    `raw_score` for a human is exempt from the value_field gate (the min-N
    dedup guarantee the ontology author already declared is the trust
    signal), mirroring DSO's `Response.value` / `deriveCurrentState`
    (AC10)."""
    registry = _contributor_registry(make_registry)
    store = make_store(registry)
    _seed_two_shelves(store)
    for i in range(3):
        store.insert("Reader", {"id": f"reader-{i}"}, SRC)
        store.insert(
            "Reading",
            {"id": f"r{i}", "shelf_id": "shelf-1", "raw_score": 4.0},
            SRC,
        )
        store.create_link("byReader", f"r{i}", f"reader-{i}")
    gq = GuardedQuery(store, registry, _contributor_policy(make_policy))

    human = _human("shelf", "shelf-1")
    result = gq.aggregate(human, "Reading", "raw_score")
    assert result == pytest.approx(4.0)


# -- count_contributors: the releasable-population size, never an identity --
#
# Spec: $CTRL/var/specs/ontary/contributor-count-api.md. The engine
# already computes this number in `_contributor_count` to enforce min-N and
# then discards it, so an author who wants to report "based on N people"
# has to count an identity field off visible rows -- which is exactly the
# respondent-disclosure trade this method exists to remove.


def test_count_contributors_counts_distinct_contributors_not_rows(
    make_registry: RegistryFactory,
    make_policy: PolicyFactory,
    make_store: StoreFactory,
) -> None:
    """3 Readings from 3 distinct Readers, one of whom submitted twice (4
    rows), counts 3 -- the same de-dup `aggregate`'s min-N gate applies."""
    registry = _contributor_registry(make_registry)
    store = make_store(registry)
    _seed_two_shelves(store)
    for i in range(3):
        store.insert("Reader", {"id": f"reader-{i}"}, SRC)
        store.insert(
            "Reading", {"id": f"r{i}", "shelf_id": "shelf-1", "score": 4.0}, SRC
        )
        store.create_link("byReader", f"r{i}", f"reader-{i}")
    # A 4th ROW from an already-counted Reader must not move the count.
    store.insert("Reading", {"id": "r3", "shelf_id": "shelf-1", "score": 4.0}, SRC)
    store.create_link("byReader", "r3", "reader-0")
    gq = GuardedQuery(store, registry, _contributor_policy(make_policy))

    assert gq.count_contributors(_human("shelf", "shelf-1"), "Reading") == 3


def test_count_contributors_scope_filters_like_aggregate(
    make_registry: RegistryFactory,
    make_policy: PolicyFactory,
    make_store: StoreFactory,
) -> None:
    """Readings on another shelf are invisible and uncounted -- the count
    must never describe a population the caller cannot read."""
    registry = _contributor_registry(make_registry)
    store = make_store(registry)
    _seed_two_shelves(store)
    for i in range(3):
        store.insert("Reader", {"id": f"reader-{i}"}, SRC)
        store.insert(
            "Reading", {"id": f"r{i}", "shelf_id": "shelf-1", "score": 4.0}, SRC
        )
        store.create_link("byReader", f"r{i}", f"reader-{i}")
    # shelf-2: a bigger population the shelf-1 consumer must not see.
    for i in range(5):
        store.insert("Reader", {"id": f"other-{i}"}, SRC)
        store.insert(
            "Reading", {"id": f"o{i}", "shelf_id": "shelf-2", "score": 1.0}, SRC
        )
        store.create_link("byReader", f"o{i}", f"other-{i}")
    gq = GuardedQuery(store, registry, _contributor_policy(make_policy))

    assert gq.count_contributors(_human("shelf", "shelf-1"), "Reading") == 3


def test_count_contributors_below_min_n_raises_never_returns_the_count(
    make_registry: RegistryFactory,
    make_policy: PolicyFactory,
    make_store: StoreFactory,
) -> None:
    """THE security pin. A sub-threshold selection must raise, not return
    `2`: handing back the exact size of a population too small to report an
    aggregate for re-opens the disclosure min-N withholding exists to
    prevent, through a new door and past the aggregate's own refusal."""
    registry = _contributor_registry(make_registry)
    store = make_store(registry)
    _seed_two_shelves(store)
    for i in range(2):
        store.insert("Reader", {"id": f"reader-{i}"}, SRC)
        store.insert(
            "Reading", {"id": f"r{i}", "shelf_id": "shelf-1", "score": 4.0}, SRC
        )
        store.create_link("byReader", f"r{i}", f"reader-{i}")
    gq = GuardedQuery(store, registry, _contributor_policy(make_policy))

    with raises_code(VisibilityError, "MIN_N_VIOLATION"):
        gq.count_contributors(_human("shelf", "shelf-1"), "Reading")


def test_count_contributors_min_n_message_does_not_echo_the_true_count(
    make_registry: RegistryFactory,
    make_policy: PolicyFactory,
    make_store: StoreFactory,
) -> None:
    """The refusal may disclose the THRESHOLD ("fewer than 3") but not the
    observed count ("exactly 2") -- otherwise the message itself is the
    oracle the raise was meant to close."""
    registry = _contributor_registry(make_registry)
    store = make_store(registry)
    _seed_two_shelves(store)
    for i in range(2):
        store.insert("Reader", {"id": f"reader-{i}"}, SRC)
        store.insert(
            "Reading", {"id": f"r{i}", "shelf_id": "shelf-1", "score": 4.0}, SRC
        )
        store.create_link("byReader", f"r{i}", f"reader-{i}")
    gq = GuardedQuery(store, registry, _contributor_policy(make_policy))

    with raises_code(VisibilityError, "MIN_N_VIOLATION") as excinfo:
        gq.count_contributors(_human("shelf", "shelf-1"), "Reading")
    message = str(excinfo.value)
    assert "min_n=3" in message
    # The observed count (2) must appear nowhere -- not as a bare digit, and
    # not in the "N contributors" phrasing `_aggregate` itself still uses.
    assert "2" not in message


def test_count_contributors_does_not_require_a_readable_contributor_field(
    make_registry: RegistryFactory,
    make_policy: PolicyFactory,
    make_store: StoreFactory,
) -> None:
    """The whole point: the count is available to a consumer who cannot read
    the identity field behind it. `raw_score` stands in for a hidden field
    here only to prove hiddenness is irrelevant to counting -- contributor
    resolution reads the raw store row, not the redacted projection."""
    registry = _contributor_registry(make_registry)
    store = make_store(registry)
    _seed_two_shelves(store)
    for i in range(3):
        store.insert("Reader", {"id": f"reader-{i}"}, SRC)
        store.insert(
            "Reading",
            {"id": f"r{i}", "shelf_id": "shelf-1", "raw_score": 4.0},
            SRC,
        )
        store.create_link("byReader", f"r{i}", f"reader-{i}")
    gq = GuardedQuery(store, registry, _contributor_policy(make_policy))

    # `human` cannot read `raw_score` (human_visible=False) and never reads
    # Reader ids at all; the count is still correct for both kinds.
    assert gq.count_contributors(_human("shelf", "shelf-1"), "Reading") == 3
    assert gq.count_contributors(_ai("shelf", "shelf-1"), "Reading") == 3


def test_count_contributors_without_contributor_rules_falls_back_to_rows(
    make_registry: RegistryFactory,
    make_policy: PolicyFactory,
    make_store: StoreFactory,
) -> None:
    """A type declaring no contributor_rules keeps the row-count floor,
    exactly as the min-N gate does -- additive, never a behaviour change."""
    registry = _contributor_registry(make_registry)
    store = make_store(registry)
    _seed_two_shelves(store)
    for i in range(3):
        store.insert(
            "Book", {"id": f"book-{i}", "shelf_id": "shelf-1", "rating": 4.0}, SRC
        )
    gq = GuardedQuery(store, registry, _contributor_policy(make_policy))

    assert gq.count_contributors(_human("shelf", "shelf-1"), "Book") == 3


def test_count_contributors_fallback_path_is_also_min_n_gated(
    make_registry: RegistryFactory,
    make_policy: PolicyFactory,
    make_store: StoreFactory,
) -> None:
    """The REFUSE direction of the row-count fallback -- the default path
    for every type that never opts into `contributor_rules`, and therefore
    the one a later refactor is most likely to leave ungated.

    Pinning only the release direction (the test above) left a mutation like
    `if count < min_n and obj_type in self._policy.contributor_rules:`
    entirely green while sub-threshold counts leaked for every ordinary
    type. Reviewer P0, measured at 1048 passing before this existed.
    """
    registry = _contributor_registry(make_registry)
    store = make_store(registry)
    _seed_two_shelves(store)
    for i in range(2):  # 2 rows against min_n=3 -- below the floor
        store.insert(
            "Book", {"id": f"book-{i}", "shelf_id": "shelf-1", "rating": 4.0}, SRC
        )
    gq = GuardedQuery(store, registry, _contributor_policy(make_policy))

    with raises_code(VisibilityError, "MIN_N_VIOLATION"):
        gq.count_contributors(_human("shelf", "shelf-1"), "Book")


def test_count_contributors_where_gate_matches_aggregate(
    make_registry: RegistryFactory,
    make_policy: PolicyFactory,
    make_store: StoreFactory,
) -> None:
    """Filtering on a hidden field is refused here exactly as it is for
    `aggregate` -- the count must not become a filter oracle either."""
    registry = _contributor_registry(make_registry)
    store = make_store(registry)
    _seed_two_shelves(store)
    for i in range(3):
        store.insert("Reader", {"id": f"reader-{i}"}, SRC)
        store.insert(
            "Reading",
            {"id": f"r{i}", "shelf_id": "shelf-1", "score": 4.0, "raw_score": 1.0},
            SRC,
        )
        store.create_link("byReader", f"r{i}", f"reader-{i}")
    gq = GuardedQuery(store, registry, _contributor_policy(make_policy))

    with raises_code(VisibilityError, "VISIBILITY_DENIED"):
        gq.count_contributors(
            _human("shelf", "shelf-1"), "Reading", where={"raw_score": 1.0}
        )


#: The two contributor-counting shapes `_contributor_count` can take, so the
#: parity sweep below covers BOTH: a type that declares `contributor_rules`
#: (distinct-identity de-dup) and one that does not (the row-count fallback,
#: which is the default for every ordinary type). Sweeping only the former
#: is what let a mutation gating just the contributor-rules path pass 1048
#: tests -- reviewer P0.
_PARITY_SHAPES = (
    ("Reading", "score"),  # declares contributor_rules
    ("Book", "rating"),  # no contributor_rules -> row-count fallback
)


@pytest.mark.parametrize("obj_type,value_field", _PARITY_SHAPES)
@pytest.mark.parametrize("min_n", [0, 1, 3])
def test_count_contributors_and_aggregate_agree_on_every_release_decision(
    obj_type: str,
    value_field: str,
    min_n: int,
    make_registry: RegistryFactory,
    make_policy: PolicyFactory,
    make_store: StoreFactory,
) -> None:
    """The class-level guard (L09): for the SAME selection, `aggregate` and
    `count_contributors` must release or refuse TOGETHER.

    Pinning one population at one threshold on one type shape would leave a
    refactor free to gate one path and not the other -- both mutations the
    reviewer landed (fallback ungated; parity true only for `min_n >= 1`)
    lived in exactly the gaps a narrower sweep left open. So this crosses
    the population boundary with both contributor-counting shapes AND the
    degenerate thresholds: this test mutates `ScopePolicy.min_n` after
    construction to reach `min_n=0`, where the empty selection used to
    diverge (`aggregate` raised via its `no_rows` branch, the count returned
    `0`). Constructor input now refuses that threshold.
    """
    for population in range(0, 5):
        registry = _contributor_registry(make_registry)
        store = make_store(registry)
        _seed_two_shelves(store)
        for i in range(population):
            if obj_type == "Reading":
                store.insert("Reader", {"id": f"reader-{i}"}, SRC)
                store.insert(
                    "Reading",
                    {"id": f"r{i}", "shelf_id": "shelf-1", "score": 4.0},
                    SRC,
                )
                store.create_link("byReader", f"r{i}", f"reader-{i}")
            else:
                store.insert(
                    "Book",
                    {"id": f"book-{i}", "shelf_id": "shelf-1", "rating": 4.0},
                    SRC,
                )
        policy = _contributor_policy(make_policy)
        policy.min_n = min_n
        gq = GuardedQuery(store, registry, policy)
        consumer = _human("shelf", "shelf-1")

        try:
            gq.aggregate(consumer, obj_type, value_field)
        except VisibilityError as exc:
            assert exc.code == "MIN_N_VIOLATION"
            aggregate_released = False
        else:
            aggregate_released = True

        try:
            count = gq.count_contributors(consumer, obj_type)
        except VisibilityError as exc:
            assert exc.code == "MIN_N_VIOLATION"
            count_released = False
        else:
            count_released = True
            assert count == population

        assert aggregate_released == count_released, (
            f"{obj_type} min_n={min_n} population={population}: aggregate "
            f"released={aggregate_released} but count "
            f"released={count_released} -- the two gates diverged"
        )


def test_count_contributors_complement_differencing_is_a_known_residual(
    make_registry: RegistryFactory,
    make_policy: PolicyFactory,
    make_store: StoreFactory,
) -> None:
    """Pins what the min-N gate does NOT close, so the guarantee in
    `count_contributors`' docstring cannot silently drift stronger than the
    code (reviewer P1; L09's claim-exceeds-code shape).

    The gate refuses to RETURN a sub-threshold count. It does not stop a
    caller DERIVING one by subtracting two released counts. Both facts are
    asserted here, together: the refusal fires, and the refused cell's exact
    size falls straight out of arithmetic on the two selections that were
    allowed.

    This is deliberately a characterization test, not an aspiration. When
    SDK M12 adds complementary suppression it MUST fail -- that failure is
    the signal to update the docstring, the CHANGELOG entry, and the spec's
    §7 residual note in the same change. A residual nobody pinned is a
    residual that quietly becomes a lie.
    """
    registry = _contributor_registry(make_registry)
    store = make_store(registry)
    _seed_two_shelves(store)
    # 10 contributors on shelf-1: 8 scoring 4.0, 2 scoring 5.0. The 2-cell
    # is below min_n=3; the other two selections clear it comfortably.
    for i in range(10):
        store.insert("Reader", {"id": f"reader-{i}"}, SRC)
        store.insert(
            "Reading",
            {
                "id": f"r{i}",
                "shelf_id": "shelf-1",
                "score": 5.0 if i < 2 else 4.0,
            },
            SRC,
        )
        store.create_link("byReader", f"r{i}", f"reader-{i}")
    gq = GuardedQuery(store, registry, _contributor_policy(make_policy))
    consumer = _human("shelf", "shelf-1")

    total = gq.count_contributors(consumer, "Reading")
    majority = gq.count_contributors(consumer, "Reading", where={"score": 4.0})
    assert (total, majority) == (10, 8)

    # The gate does its direct job...
    with raises_code(VisibilityError, "MIN_N_VIOLATION"):
        gq.count_contributors(consumer, "Reading", where={"score": 5.0})

    # ...and is nonetheless differenced away. RESIDUAL, not a passing grade.
    assert total - majority == 2
