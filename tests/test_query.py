"""Integration tests for `ontary.query.GuardedQuery` on a toy ontology.

The toy ontology is a small "library" domain (Library/Shelf/Book/Loan),
matching the style of `tests/test_scope.py` -- deliberately NOT named after
DSO. It is extended here with sensitivity-tagged properties (an
`ai_usable=False` shelf note, a `human_visible=False` book acquisition
cost) and an `identity_revealing` link (Loan -> Borrower) so redaction and
identity-revealing traversal denial can be exercised end to end.
"""

from __future__ import annotations

import inspect
import json
from collections.abc import Callable
from datetime import date
from itertools import product
from typing import Any

import pytest
from conftest import raises_code

import ontary.query as query_module
from ontary import _typed_api
from ontary.actions import ActionContext, ActionExecutor
from ontary.authoring import ActionParams
from ontary.client import OntologyClient, OntologyRuntime
from ontary.errors import InternalError, ValidationFailed, VisibilityError
from ontary.functions import BoundQuery
from ontary.meta import (
    ActionParameterDef,
    ActionTypeDef,
    Cardinality,
    LinkTypeDef,
    ObjectTypeDef,
    OntologyRegistry,
    PropertyDef,
    Sensitivity,
)
from ontary.ontology import OntologyDef
from ontary.query import _AUTHOR_DISPATCH, GuardedQuery, _AuthorDispatch
from ontary.scope import (
    CustomResolver,
    DirectProperty,
    RowVisibilityStore,
    ScopePolicy,
    SelfScope,
    ViaLink,
    resolve_owning_scope,
)
from ontary.security import Consumer, covers_scope
from ontary.store import InMemoryStore, ObjectStore, Source, Store, StoredObject

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
            properties=[
                PropertyDef(name="id", type="str"),
                PropertyDef(name="shelf_id", type="str", required=False),
            ],
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
            api_name="relatedShelf",
            from_type="Book",
            to_type="Shelf",
            cardinality=Cardinality.MANY_TO_MANY,
            description="Book -> related shelves",
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
                ViaLink(link_api_name="inLibrary", direction="from", parent_type="Library"),
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


def _operator_registry(make_registry: RegistryFactory) -> OntologyRegistry:
    return make_registry(
        object_types=[
            ObjectTypeDef(
                api_name="Value",
                display_name="Value",
                description="A row covering every declared scalar type",
                layer="L0",
                properties=[
                    PropertyDef(name="id", type="str"),
                    PropertyDef(name="text", type="str", required=False),
                    PropertyDef(name="count", type="int", required=False),
                    PropertyDef(name="ratio", type="float", required=False),
                    PropertyDef(name="flag", type="bool", required=False),
                    PropertyDef(name="day", type="date", required=False),
                    PropertyDef(name="moment", type="datetime", required=False),
                    PropertyDef(name="data", type="json", required=False),
                ],
                primary_key="id",
            )
        ]
    )


def _operator_query(
    make_registry: RegistryFactory,
    make_policy: PolicyFactory,
    make_store: StoreFactory,
) -> GuardedQuery:
    registry = _operator_registry(make_registry)
    store = make_store(registry)
    rows = [
        {
            "id": "r1",
            "text": "alpha needle",
            "count": 1,
            "ratio": 1.5,
            "flag": True,
            "day": "2026-01-15",
            "moment": "2026-01-15T00:00:00+00:00",
            "data": {"kind": "a"},
        },
        {
            "id": "r2",
            "text": "beta",
            "count": 2,
            "ratio": 2.5,
            "flag": False,
            "day": "2026-02-15",
            "moment": "2026-02-15T00:00:00+00:00",
            "data": {"kind": "b"},
        },
        {
            "id": "r3",
            "text": "gamma needle",
            "count": 3,
            "ratio": 3.5,
            "flag": True,
            "day": "2026-03-15",
            "moment": "2026-03-15T00:00:00+00:00",
            "data": {"kind": "a"},
        },
    ]
    for row in rows:
        store.insert("Value", row, SRC)
    policy = make_policy(levels=["team"], unscoped_types={"Value"}, min_n=1)
    return GuardedQuery(store, registry, policy)


_VALID_OPERATOR_CASES = [
    ("gt", "count", 1, ["r2", "r3"]),
    ("gte", "count", 2, ["r2", "r3"]),
    ("lt", "count", 3, ["r1", "r2"]),
    ("lte", "count", 2, ["r1", "r2"]),
    ("gt", "ratio", 2.0, ["r2", "r3"]),
    ("gte", "ratio", 2.5, ["r2", "r3"]),
    ("lt", "ratio", 3.5, ["r1", "r2"]),
    ("lte", "ratio", 2.5, ["r1", "r2"]),
    ("gt", "day", date(2026, 1, 15), ["r2", "r3"]),
    ("gte", "day", date(2026, 2, 15), ["r2", "r3"]),
    ("lt", "day", date(2026, 3, 15), ["r1", "r2"]),
    ("lte", "day", date(2026, 2, 15), ["r1", "r2"]),
    ("in", "text", ["alpha needle"], ["r1"]),
    ("in", "count", [1, 3], ["r1", "r3"]),
    ("in", "ratio", [1.5, 3.5], ["r1", "r3"]),
    ("in", "flag", [True], ["r1", "r3"]),
    ("in", "day", [date(2026, 2, 15)], ["r2"]),
    ("in", "moment", ["2026-02-15T00:00:00+00:00"], ["r2"]),
    ("in", "data", [{"kind": "a"}], ["r1", "r3"]),
    ("ne", "text", "alpha needle", ["r2", "r3"]),
    ("ne", "count", 2, ["r1", "r3"]),
    ("ne", "ratio", 2.5, ["r1", "r3"]),
    ("ne", "flag", False, ["r1", "r3"]),
    ("ne", "day", date(2026, 2, 15), ["r1", "r3"]),
    ("ne", "moment", "2026-02-15T00:00:00+00:00", ["r1", "r3"]),
    ("ne", "data", {"kind": "b"}, ["r1", "r3"]),
    ("contains", "text", "needle", ["r1", "r3"]),
]


@pytest.mark.parametrize(("operator", "field", "operand", "expected_ids"), _VALID_OPERATOR_CASES)
def test_where_operator_matrix_matches_declared_types(
    make_registry: RegistryFactory,
    make_policy: PolicyFactory,
    make_store: StoreFactory,
    operator: str,
    field: str,
    operand: object,
    expected_ids: list[str],
) -> None:
    query = _operator_query(make_registry, make_policy, make_store)

    rows = query.get_objects(
        _human("shelf", "shelf-1"),
        "Value",
        where={field: {operator: operand}},
        limit=None,
    )

    assert [row.payload["id"] for row in rows] == expected_ids


@pytest.mark.parametrize(
    ("where", "code"),
    [
        ({"text": {"regex": "needle"}}, "UNKNOWN_OPERATOR"),
        ({"text": {"gt": "needle"}}, "OPERATOR_TYPE_MISMATCH"),
        ({"flag": {"gt": False}}, "OPERATOR_TYPE_MISMATCH"),
        ({"moment": {"gt": "2026-01-01T00:00:00+00:00"}}, "OPERATOR_TYPE_MISMATCH"),
        ({"data": {"contains": "kind"}}, "OPERATOR_TYPE_MISMATCH"),
        ({"count": {"in": 1}}, "OPERATOR_TYPE_MISMATCH"),
        ({"count": {"in": [True]}}, "OPERATOR_TYPE_MISMATCH"),
        ({"count": {"ne": "2"}}, "OPERATOR_TYPE_MISMATCH"),
    ],
)
def test_where_operator_mismatch_is_catalogued_refusal(
    make_registry: RegistryFactory,
    make_policy: PolicyFactory,
    make_store: StoreFactory,
    where: dict[str, object],
    code: str,
) -> None:
    query = _operator_query(make_registry, make_policy, make_store)

    with raises_code(ValidationFailed, code):
        query.get_objects(_human("shelf", "shelf-1"), "Value", where=where)


@pytest.mark.parametrize(
    "where",
    [
        {"count": "2"},
        {"count": True},
        {"count": 1.0},
        {"count": [1, 2]},
        {"flag": 1},
        {"flag": "true"},
        {"ratio": True},
        {"text": 1},
        {"day": "2026-1-15"},
        {"moment": 1},
    ],
)
def test_bare_equality_operand_is_type_checked_like_every_operator(
    make_registry: RegistryFactory,
    make_policy: PolicyFactory,
    make_store: StoreFactory,
    where: dict[str, object],
) -> None:
    """A bare operand is an `eq` operand, and gets the same type check.

    The input the suite could not previously construct: EVERY operand-type
    case above this one uses the MAPPING form, so "a bare operand of the
    wrong declared type" had no test to fail. `eq` is not in
    `_WHERE_OPERATORS` -- the bare form is the only equality spelling -- so
    `_compile_where_clause` returned straight out of its `bare` branch
    without ever reaching `_validate_where_operator`.

    Two of these were not merely unvalidated, they returned the WRONG ROWS.
    Python's `bool` is an `int` subclass, so `where={"count": True}` matched
    `count == 1` and `where={"flag": 1}` matched every `flag is True` row --
    silently, on all six read surfaces. `validate_scalar` rejects exactly
    this ("bool is never accepted for a non-'bool' declared type"); the bare
    path never called it.
    """
    query = _operator_query(make_registry, make_policy, make_store)

    with raises_code(ValidationFailed, "OPERATOR_TYPE_MISMATCH"):
        query.get_objects(_human("shelf", "shelf-1"), "Value", where=where)


@pytest.mark.parametrize(
    ("where", "expected_ids"),
    [
        ({"count": 1}, ["r1"]),
        ({"text": "beta"}, ["r2"]),
        ({"flag": False}, ["r2"]),
        ({"ratio": 1.5}, ["r1"]),
        # int on a float property is declared-type widening, not a mismatch:
        # `PYTHON_TYPES["float"]` is `(float, int)`, and the mapping form
        # accepts it too. Tightening `eq` must not narrow this.
        ({"ratio": 2}, []),
        ({"day": "2026-01-15"}, ["r1"]),
        ({"day": date(2026, 2, 15)}, ["r2"]),
    ],
)
def test_a_well_typed_bare_operand_still_selects(
    make_registry: RegistryFactory,
    make_policy: PolicyFactory,
    make_store: StoreFactory,
    where: dict[str, object],
    expected_ids: list[str],
) -> None:
    """The companion to the refusal above: what must NOT start refusing.

    A guard that refuses everything passes the test above. These are the
    shapes the tightening has to leave alone -- including the int-on-float
    widening and both spellings of a date operand, which normalize through
    `_normalize_where_operand` rather than through the type check.
    """
    query = _operator_query(make_registry, make_policy, make_store)

    rows = query.get_objects(_human("shelf", "shelf-1"), "Value", where=where, limit=None)

    assert [row.payload["id"] for row in rows] == expected_ids


def test_bare_operand_refusal_reaches_every_read_surface(
    make_registry: RegistryFactory,
    make_policy: PolicyFactory,
    make_store: StoreFactory,
) -> None:
    """All six read surfaces compile `where` through one path, so all six
    refuse. Measured before the fix, all six ACCEPTED `{"count": True}` and
    answered off `count == 1`: rows ['r1'], count 1, exists True, aggregate
    1.5, aggregate_by {'shelf-1': 1.5}, count_contributors 1. A parity pin
    on the gate already exists; this is the parity pin on the operand.
    """
    query = _operator_query(make_registry, make_policy, make_store)
    consumer = _human("shelf", "shelf-1")
    where = {"count": True}

    for call in (
        lambda: query.get_objects(consumer, "Value", where=where, limit=None),
        lambda: query.count(consumer, "Value", where=where),
        lambda: query.exists(consumer, "Value", where=where),
        lambda: query.aggregate(consumer, "Value", "ratio", where=where),
        lambda: query.aggregate_by(consumer, "Value", "ratio", "text", where=where),
        lambda: query.count_contributors(consumer, "Value", where=where),
    ):
        with raises_code(ValidationFailed, "OPERATOR_TYPE_MISMATCH"):
            call()


def test_where_field_gates_run_before_operator_evaluation(
    make_registry: RegistryFactory,
    make_policy: PolicyFactory,
    make_store: StoreFactory,
) -> None:
    query = _operator_query(make_registry, make_policy, make_store)

    with raises_code(ValidationFailed, "UNKNOWN_FIELD"):
        query.get_objects(
            _human("shelf", "shelf-1"),
            "Value",
            where={"not_declared": {"gt": 1}},
        )


def test_aggregate_uses_the_same_operator_evaluator(
    make_registry: RegistryFactory,
    make_policy: PolicyFactory,
    make_store: StoreFactory,
) -> None:
    query = _operator_query(make_registry, make_policy, make_store)

    result = query.aggregate(
        _human("shelf", "shelf-1"),
        "Value",
        "ratio",
        where={"day": {"gte": date(2026, 2, 15)}},
    )

    assert result == pytest.approx(3.0)


def test_count_and_exists_match_the_same_unbounded_visible_selection(
    make_registry: RegistryFactory,
    make_policy: PolicyFactory,
    make_store: StoreFactory,
) -> None:
    """Changing either method to ignore ``where`` or count a different row
    shelf must disagree with the literal two-row visible read here."""
    query = _operator_query(make_registry, make_policy, make_store)
    consumer = _human("shelf", "shelf-1")
    where = {"count": {"gte": 2}}

    rows = query.get_objects(consumer, "Value", where=where, limit=None)

    assert len(rows) == 2
    assert query.count(consumer, "Value", where=where) == len(rows)
    assert query.exists(consumer, "Value", where=where) is True
    assert query.exists(consumer, "Value", where={"count": {"gt": 99}}) is False


def test_exists_is_false_for_an_empty_type(
    make_registry: RegistryFactory,
    make_policy: PolicyFactory,
    make_store: StoreFactory,
) -> None:
    registry = _operator_registry(make_registry)
    store = make_store(registry)
    query = GuardedQuery(
        store,
        registry,
        make_policy(levels=["team"], unscoped_types={"Value"}, min_n=1),
    )
    consumer = _human("team", "team-1")

    assert query.count(consumer, "Value") == 0
    assert query.exists(consumer, "Value") is False


def test_exists_is_false_when_where_matches_nothing(
    make_registry: RegistryFactory,
    make_policy: PolicyFactory,
    make_store: StoreFactory,
) -> None:
    query = _operator_query(make_registry, make_policy, make_store)
    consumer = _human("team", "team-1")
    where = {"count": {"gt": 99}}

    assert query.count(consumer, "Value", where=where) == 0
    assert query.exists(consumer, "Value", where=where) is False


def test_exists_is_false_when_every_row_is_hidden_by_scope(
    make_registry: RegistryFactory,
    make_policy: PolicyFactory,
    make_store: StoreFactory,
) -> None:
    registry = _library_registry(make_registry)
    store = make_store(registry)
    _seed_two_shelves(store)
    store.insert("Book", {"id": "book-2", "shelf_id": "shelf-2"}, SRC)
    query = GuardedQuery(store, registry, _library_policy(make_policy))
    consumer = _human("shelf", "shelf-1")

    assert query.count(consumer, "Book") == 0
    assert query.exists(consumer, "Book") is False


def test_exists_matches_count_for_a_min_n_policy_selection(
    make_registry: RegistryFactory,
    make_policy: PolicyFactory,
    make_store: StoreFactory,
) -> None:
    registry = _library_registry(make_registry)
    store = make_store(registry)
    _seed_two_shelves(store)
    store.insert("Book", {"id": "book-1", "shelf_id": "shelf-1"}, SRC)
    query = GuardedQuery(store, registry, _library_policy(make_policy))
    consumer = _human("shelf", "shelf-1")

    assert query.count(consumer, "Book") == 1
    assert query.exists(consumer, "Book") is True


_BOOK_SHAPE_ROWS: dict[str, dict[str, Any]] = {
    "V": {"shelf_id": "shelf-1", "rating": 4.0},
    "S": {"shelf_id": "shelf-2", "rating": 4.0},
    "R": {"shelf_id": "shelf-1", "rating": 4.0, "internal_score": 0.0},
    "W": {"shelf_id": "shelf-1", "rating": 0.0},
}


def _book_shape_row_is_visible(
    _store: RowVisibilityStore,
    _consumer: Consumer,
    _obj_type: str,
    payload: dict[str, Any],
) -> bool:
    return payload.get("internal_score") != 0.0


@pytest.mark.parametrize(
    ("kind", "passes_scope", "passes_row_visibility", "passes_where"),
    [
        pytest.param("V", True, True, True, id="visible"),
        pytest.param("S", False, True, True, id="scope-hidden"),
        pytest.param("R", True, False, True, id="row-visibility-hidden"),
        pytest.param("W", True, True, False, id="where-hidden"),
    ],
)
def test_generated_book_shape_kind_reaches_its_intended_gate(
    kind: str,
    passes_scope: bool,
    passes_row_visibility: bool,
    passes_where: bool,
    make_registry: RegistryFactory,
    make_policy: PolicyFactory,
    make_store: StoreFactory,
) -> None:
    """Pin each alphabet kind to the rejection gate it is meant to test."""
    registry = _library_registry(make_registry)
    store = make_store(registry)
    _seed_two_shelves(store)
    book_id = f"book-{kind}"
    store.insert(
        "Book",
        {"id": book_id, **_BOOK_SHAPE_ROWS[kind]},
        SRC,
    )
    policy = _library_policy(make_policy)
    policy.row_visibility = {"Book": _book_shape_row_is_visible}
    query = GuardedQuery(store, registry, policy)
    consumer = _human("shelf", "shelf-1")
    row = store.read_current("Book", book_id)
    assert row is not None

    no_where = query._compile_where(
        "Book", query._require_coherent_scope("Book", where=None).where
    )
    rating_where = query._compile_where(
        "Book",
        query._require_coherent_scope(
            "Book", where={"rating": {"gte": 1.0}}
        ).where,
    )
    resolved_scope = resolve_owning_scope(policy, store, "Book", book_id)
    row_visibility = policy.row_visibility["Book"]

    assert no_where is None
    assert covers_scope(policy, consumer, resolved_scope) is passes_scope
    assert (
        row_visibility(RowVisibilityStore(store), consumer, "Book", row.payload)
        is passes_row_visibility
    )
    assert rating_where is not None
    assert rating_where(row.payload) is passes_where


@pytest.mark.parametrize(
    ("where_id", "where"),
    [
        ("no-where", None),
        ("rating-gte-1", {"rating": {"gte": 1.0}}),
    ],
    ids=["no-where", "rating-gte-1"],
)
@pytest.mark.parametrize("length", range(6), ids=lambda length: f"length-{length}")
def test_exists_bound_matches_unbounded_walk_over_generated_row_shapes(
    length: int,
    where_id: str,
    where: dict[str, Any] | None,
    make_registry: RegistryFactory,
    make_policy: PolicyFactory,
    make_store: StoreFactory,
) -> None:
    """Enumerate every bounded walk shape through all three rejection gates.

    ``V`` is visible, ``S`` is scope-hidden, ``R`` is row-visibility-hidden,
    and ``W`` is rejected only by the non-None ``where`` arm. The unbounded
    read is the oracle for both bounded ``exists`` and unbounded ``count``.

    This family covers every sequence over VSRW of length 0..5. A bound defect
    that first triggers only when 5+ rejected rows precede the first visible
    row is not covered.
    """
    consumer = _human("shelf", "shelf-1")

    for row_kinds in product("VSRW", repeat=length):
        shape = "".join(row_kinds)
        registry = _library_registry(make_registry)
        store = make_store(registry)
        _seed_two_shelves(store)
        for index, kind in enumerate(row_kinds):
            store.insert(
                "Book",
                {"id": f"book-{index}", **_BOOK_SHAPE_ROWS[kind]},
                SRC,
            )
        policy = _library_policy(make_policy)
        policy.row_visibility = {"Book": _book_shape_row_is_visible}
        query = GuardedQuery(store, registry, policy)
        case = f"shape={shape or '<empty>'}, where={where_id}"

        truth = query.get_objects(consumer, "Book", where, limit=None)
        assert query.exists(consumer, "Book", where) is (truth != []), case
        assert query.count(consumer, "Book", where) == len(truth), case


@pytest.mark.parametrize(
    ("where_id", "where"),
    [
        ("no-where", None),
        ("rating-gte-1", {"rating": {"gte": 1.0}}),
    ],
    ids=["no-where", "rating-gte-1"],
)
@pytest.mark.parametrize("length", range(6), ids=lambda length: f"length-{length}")
def test_scan_report_counts_generated_row_shapes(
    length: int,
    where_id: str,
    where: dict[str, Any] | None,
    make_registry: RegistryFactory,
    make_policy: PolicyFactory,
    make_store: StoreFactory,
) -> None:
    """Count scan outcomes over every VSRW sequence of length 0..5.

    ``rows_scanned`` counts every raw row consumed by the unbounded walk,
    ``rows_returned`` counts visible rows that pass ``where``, and
    ``rows_hidden_by_scope`` counts ``S`` but not ``R``. A counter defect
    that first miscounts only once 5+ rejected rows precede the first visible
    row is not covered by this family.
    """
    consumer = _human("shelf", "shelf-1")

    for row_kinds in product("VSRW", repeat=length):
        shape = "".join(row_kinds)
        registry = _library_registry(make_registry)
        store = make_store(registry)
        _seed_two_shelves(store)
        for index, kind in enumerate(row_kinds):
            store.insert(
                "Book",
                {"id": f"book-{index}", **_BOOK_SHAPE_ROWS[kind]},
                SRC,
            )
        policy = _library_policy(make_policy)
        policy.row_visibility = {"Book": _book_shape_row_is_visible}
        runtime = OntologyRuntime(
            OntologyDef(name="library", registry=registry, policy=policy),
            store,
        )
        case = f"shape={shape or '<empty>'}, where={where_id}"

        truth = runtime.query.get_objects(
            consumer, "Book", where, limit=None
        )
        report = runtime.explain_scan(consumer, "Book", where)
        assert report.rows_returned == len(truth), case
        assert report.rows_scanned == len(store.read_all("Book")), case
        assert report.rows_hidden_by_scope == row_kinds.count("S"), case
        if "S" in row_kinds and "R" in row_kinds:
            assert report.rows_hidden_by_scope != (
                row_kinds.count("S") + row_kinds.count("R")
            ), case


def test_exists_is_true_when_the_first_row_matches(
    make_registry: RegistryFactory,
    make_policy: PolicyFactory,
    make_store: StoreFactory,
) -> None:
    query = _operator_query(make_registry, make_policy, make_store)
    consumer = _human("team", "team-1")

    assert query.count(consumer, "Value", where={"id": "r1"}) == 1
    assert query.exists(consumer, "Value", where={"id": "r1"}) is True


def _count_and_exists_store_reads(
    make_registry: RegistryFactory,
    make_policy: PolicyFactory,
    make_store: StoreFactory,
    monkeypatch: pytest.MonkeyPatch,
    book_count: int,
) -> tuple[int, int]:
    """Store reads spent by ``count`` then by ``exists`` over ``book_count`` books.

    Every book is visible, so the first row already answers ``exists``. A
    ``row_visibility`` predicate that reads a link makes each row's walk cost
    one observable store read, which is what lets the caller tell "walked one
    row" apart from "walked two".
    """
    registry = _library_registry(make_registry)
    store = make_store(registry)
    _seed_two_shelves(store)
    for index in range(1, book_count + 1):
        book_id = f"book-{index}"
        store.insert("Book", {"id": book_id, "shelf_id": "shelf-1"}, SRC)
        store.create_link("relatedShelf", book_id, "shelf-1")

    original_links_from = store.links_from
    store_calls = 0

    def counting_links_from(link: str, obj_id: str) -> list[str]:
        nonlocal store_calls
        store_calls += 1
        return original_links_from(link, obj_id)

    def visible_when_related_to_shelf(
        row_store: RowVisibilityStore,
        _consumer: Consumer,
        _obj_type: str,
        row: dict[str, Any],
    ) -> bool:
        return bool(row_store.links_from("relatedShelf", str(row["id"])))

    monkeypatch.setattr(store, "links_from", counting_links_from)
    policy = _library_policy(make_policy)
    policy.row_visibility = {"Book": visible_when_related_to_shelf}
    query = GuardedQuery(store, registry, policy)
    consumer = _human("shelf", "shelf-1")

    assert query.count(consumer, "Book") == book_count
    count_store_calls = store_calls
    store_calls = 0

    assert query.exists(consumer, "Book") is True
    return count_store_calls, store_calls


def test_exists_stops_store_reads_after_the_first_visible_row(
    make_registry: RegistryFactory,
    make_policy: PolicyFactory,
    make_store: StoreFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``exists`` reads the first visible row and no further one.

    The counts are exact on purpose. A ``<`` comparison against ``count``
    cannot tell "stopped at the first row" from "stopped one row late": both
    sit under the unbounded walk's total. Doubling the population pins the
    same claim structurally -- ``count`` grows with the row count while
    ``exists`` does not move at all.
    """
    three_books = _count_and_exists_store_reads(
        make_registry, make_policy, make_store, monkeypatch, book_count=3
    )
    six_books = _count_and_exists_store_reads(
        make_registry, make_policy, make_store, monkeypatch, book_count=6
    )

    # (count reads, exists reads). Per book: one scope read plus one
    # row_visibility read; the shared parent shelf is read once per call.
    assert three_books == (7, 3)
    assert six_books == (13, 3)


@pytest.mark.parametrize("method_name", ["count", "exists"])
def test_count_and_exists_keep_where_refusal_shape(
    method_name: str,
    make_registry: RegistryFactory,
    make_policy: PolicyFactory,
    make_store: StoreFactory,
) -> None:
    """A second operator path would commonly turn this into a bare error or
    accept it; both probes must retain T7's catalogued refusal."""
    query = _operator_query(make_registry, make_policy, make_store)

    with raises_code(ValidationFailed, "UNKNOWN_OPERATOR"):
        getattr(query, method_name)(
            _human("shelf", "shelf-1"),
            "Value",
            where={"count": {"between": [1, 2]}},
        )


def _seed_two_shelves(store: Store) -> None:
    store.insert("Library", {"id": "lib-1"}, SRC)
    store.insert("Library", {"id": "lib-2"}, SRC)
    store.insert("Shelf", {"id": "shelf-1", "note": "fiction"}, SRC)
    store.insert("Shelf", {"id": "shelf-2", "note": "nonfiction"}, SRC)
    store.create_link("inLibrary", "shelf-1", "lib-1")
    store.create_link("inLibrary", "shelf-2", "lib-2")


def test_count_and_exists_return_empty_values_when_rows_are_scoped_away(
    make_registry: RegistryFactory,
    make_policy: PolicyFactory,
    make_store: StoreFactory,
) -> None:
    """The visible shelf is empty while a raw row exists elsewhere. Returning
    one would count pre-visibility rows; raising would incorrectly min-N-gate
    ordinary visible-row counting at this policy's threshold of three."""
    registry = _library_registry(make_registry)
    store = make_store(registry)
    _seed_two_shelves(store)
    store.insert("Book", {"id": "book-2", "shelf_id": "shelf-2"}, SRC)
    query = GuardedQuery(store, registry, _library_policy(make_policy))
    consumer = _human("shelf", "shelf-1")

    rows = query.get_objects(consumer, "Book", limit=None)

    assert rows == []
    assert query.count(consumer, "Book") == len(rows) == 0
    assert query.exists(consumer, "Book") is False


@pytest.mark.parametrize("method_name", ["count", "exists"])
def test_count_and_exists_run_hidden_field_gate_before_operator_validation(
    method_name: str,
    make_registry: RegistryFactory,
    make_policy: PolicyFactory,
    make_store: StoreFactory,
) -> None:
    registry = _library_registry(make_registry)
    store = make_store(registry)
    _seed_two_shelves(store)
    query = GuardedQuery(store, registry, _library_policy(make_policy))

    with raises_code(VisibilityError, "VISIBILITY_DENIED"):
        getattr(query, method_name)(
            _human("shelf", "shelf-1"),
            "Book",
            where={"acquisition_cost": {"between": [1, 2]}},
        )


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
    results = gq.get_objects(consumer, "Book", limit=None)
    assert [r.payload["id"] for r in results] == ["book-1"]


def test_one_guarded_read_reuses_engine_parent_row_reads(
    make_registry: RegistryFactory,
    make_policy: PolicyFactory,
    make_store: StoreFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A missing or per-row cache re-reads ``shelf-1`` for both books."""
    registry = _library_registry(make_registry)
    store = make_store(registry)
    _seed_two_shelves(store)
    for book_id in ("book-1", "book-2"):
        store.insert("Book", {"id": book_id}, SRC)
        store.create_link("onShelf", book_id, "shelf-1")

    original_read_current = store.read_current
    reads: list[tuple[str, str]] = []

    def counting_read_current(obj_type: str, obj_id: str) -> StoredObject | None:
        reads.append((obj_type, obj_id))
        return original_read_current(obj_type, obj_id)

    monkeypatch.setattr(store, "read_current", counting_read_current)
    query = GuardedQuery(store, registry, _library_policy(make_policy))

    rows = query.get_objects(_human("shelf", "shelf-1"), "Book", limit=None)

    assert [row.payload["id"] for row in rows] == ["book-1", "book-2"]
    assert reads.count(("Shelf", "shelf-1")) == 1


def test_scope_cache_key_keeps_parent_object_ids_separate(
    make_registry: RegistryFactory,
    make_policy: PolicyFactory,
    make_store: StoreFactory,
) -> None:
    """Dropping ``obj_id`` from a link key discloses ``book-2``."""
    registry = _library_registry(make_registry)
    store = make_store(registry)
    _seed_two_shelves(store)
    for book_id, shelf_id in (("book-1", "shelf-1"), ("book-2", "shelf-2")):
        store.insert("Book", {"id": book_id}, SRC)
        store.create_link("onShelf", book_id, shelf_id)
    query = GuardedQuery(store, registry, _library_policy(make_policy))

    rows = query.get_objects(_human("shelf", "shelf-1"), "Book", limit=None)

    assert [row.payload["id"] for row in rows] == ["book-1"]


def test_guarded_read_hides_row_when_absent_parent_id_matches_scanned_row(
    make_registry: RegistryFactory,
    make_policy: PolicyFactory,
    make_store: StoreFactory,
) -> None:
    """Dropping ``obj_type`` from ``read_current`` discloses ``book-y``."""
    registry = _library_registry(make_registry)
    store = make_store(registry)
    store.insert("Book", {"id": "book-x"}, SRC)
    store.insert("Book", {"id": "book-y"}, SRC)
    store.create_link("onShelf", "book-y", "book-x")
    query = GuardedQuery(store, registry, _library_policy(make_policy))

    rows = query.get_objects(_human("shelf", "book-x"), "Book", limit=None)

    assert rows == []


def test_scope_cache_lifetime_is_exactly_one_guarded_read(
    make_registry: RegistryFactory,
    make_policy: PolicyFactory,
    make_store: StoreFactory,
) -> None:
    """A cache retained on ``GuardedQuery`` keeps the old library owner."""
    registry = _library_registry(make_registry)
    store = make_store(registry)
    _seed_two_shelves(store)
    store.insert("Book", {"id": "book-1", "shelf_id": "shelf-1"}, SRC)
    query = GuardedQuery(store, registry, _library_policy(make_policy))
    library_1 = _human("library", "lib-1")
    library_2 = _human("library", "lib-2")

    first = query.get_objects(library_1, "Book", limit=None)
    assert [row.payload["id"] for row in first] == ["book-1"]

    assert store.close_link("inLibrary", "shelf-1", "lib-1") is True
    store.create_link("inLibrary", "shelf-1", "lib-2")

    assert query.get_objects(library_1, "Book", limit=None) == []
    second = query.get_objects(library_2, "Book", limit=None)
    assert [row.payload["id"] for row in second] == ["book-1"]


def test_row_visibility_keeps_raw_store_identity_and_call_count_with_cache(
    make_registry: RegistryFactory,
    make_policy: PolicyFactory,
    make_store: StoreFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cache-backed row store deduplicates the predicate's own link reads."""
    registry = _library_registry(make_registry)
    store = make_store(registry)
    _seed_two_shelves(store)
    for book_id in ("book-1", "book-2"):
        store.insert(
            "Book",
            {"id": book_id, "shelf_id": "shelf-1", "rating": 4.0},
            SRC,
        )
        store.create_link("onShelf", book_id, "shelf-1")

    original_links_from = store.links_from
    underlying_calls = 0
    predicate_calls: list[int] = []
    observed_row_stores: list[RowVisibilityStore] = []

    def counting_links_from(link: str, obj_id: str) -> list[str]:
        nonlocal underlying_calls
        underlying_calls += 1
        return original_links_from(link, obj_id)

    def visible_when_shelf_has_library(
        row_store: RowVisibilityStore,
        _consumer: Consumer,
        _obj_type: str,
        row: dict[str, Any],
    ) -> bool:
        observed_row_stores.append(row_store)
        before = underlying_calls
        visible = bool(row_store.links_from("inLibrary", str(row["shelf_id"])))
        predicate_calls.append(underlying_calls - before)
        return visible

    monkeypatch.setattr(store, "links_from", counting_links_from)
    policy = _library_policy(make_policy)
    policy.row_visibility = {"Book": visible_when_shelf_has_library}
    query = GuardedQuery(store, registry, policy)
    consumer = _human("shelf", "shelf-1")

    cached_rows = query.get_objects(consumer, "Book", limit=None)
    cached_calls = list(predicate_calls)
    cached_row_stores = list(observed_row_stores)

    predicate_calls.clear()
    observed_row_stores.clear()
    monkeypatch.setattr(query_module, "_ScopeReadCache", lambda: None)
    uncached_rows = query.get_objects(consumer, "Book", limit=None)

    assert [row.payload["id"] for row in cached_rows] == ["book-1", "book-2"]
    assert [row.payload["id"] for row in uncached_rows] == ["book-1", "book-2"]
    assert cached_calls == predicate_calls == [1, 1]
    assert all(row_store._store is store for row_store in cached_row_stores)
    assert all(row_store._store is store for row_store in observed_row_stores)


def _jsonable_read_result(value: object) -> object:
    if isinstance(value, StoredObject):
        return value.model_dump(mode="json")
    if isinstance(value, list):
        return [_jsonable_read_result(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _jsonable_read_result(item) for key, item in value.items()}
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        return model_dump(mode="json")
    return value


def _read_result_bytes(call: Callable[[], object]) -> bytes:
    try:
        result: object = {"ok": _jsonable_read_result(call())}
    except (ValidationFailed, VisibilityError) as exc:
        result = {
            "error": type(exc).__name__,
            "code": exc.code,
            "message": str(exc),
        }
    return json.dumps(result, sort_keys=True, separators=(",", ":")).encode()


def _every_guarded_read_surface(
    query: GuardedQuery, consumer: Consumer, target_id: str
) -> dict[str, bytes]:
    return {
        "get_object": _read_result_bytes(lambda: query.get_object(consumer, "Book", target_id)),
        "get_objects_unbounded": _read_result_bytes(
            lambda: query.get_objects(consumer, "Book", limit=None)
        ),
        "get_objects_page": _read_result_bytes(
            lambda: query.get_objects(consumer, "Book", limit=1)
        ),
        "get_objects_ordered": _read_result_bytes(
            lambda: query.get_objects(consumer, "Book", limit=1, order_by="id")
        ),
        "count": _read_result_bytes(lambda: query.count(consumer, "Book")),
        "exists": _read_result_bytes(lambda: query.exists(consumer, "Book")),
        "traverse": _read_result_bytes(lambda: query.traverse(consumer, "onShelf", target_id)),
        "aggregate": _read_result_bytes(lambda: query.aggregate(consumer, "Book", "rating")),
        "aggregate_by": _read_result_bytes(
            lambda: query.aggregate_by(consumer, "Book", "rating", "id")
        ),
        "count_contributors": _read_result_bytes(
            lambda: query.count_contributors(consumer, "Book")
        ),
    }


def _seed_byte_identity_case(store: Store, case: str, target_id: str) -> int:
    """Seed one cache-identity case and answer the `min_n` it needs."""
    if case == "scoped-hit":
        store.insert("Book", {"id": target_id, "rating": 4.0}, SRC)
        store.create_link("onShelf", target_id, "shelf-1")
    elif case == "scoped-miss":
        store.insert("Book", {"id": target_id, "rating": 4.0}, SRC)
        store.create_link("onShelf", target_id, "shelf-2")
    elif case == "retired-target":
        store.insert("Book", {"id": target_id, "rating": 4.0}, SRC)
        store.create_link("onShelf", target_id, "shelf-1")
        store.retire_object("Book", target_id)
    elif case == "unresolved-scope":
        store.insert("Book", {"id": target_id, "rating": 4.0}, SRC)
    elif case == "min-n-gated-aggregate":
        for book_id, rating in ((target_id, 4.0), ("book-second", 2.0)):
            store.insert("Book", {"id": book_id, "rating": rating}, SRC)
            store.create_link("onShelf", book_id, "shelf-1")
        return 3
    elif case == "via-link-to-side":
        store.insert("Book", {"id": target_id, "rating": 4.0}, SRC)
        store.insert("Loan", {"id": "loan-1", "shelf_id": "shelf-1"}, SRC)
        store.create_link("loanedBook", "loan-1", target_id)
    else:
        for book_id, rating in ((target_id, 4.0), ("book-hidden", 2.0)):
            store.insert(
                "Book",
                {"id": book_id, "shelf_id": "shelf-1", "rating": rating},
                SRC,
            )
            store.create_link("onShelf", book_id, "shelf-1")
        store.create_link("relatedShelf", target_id, "shelf-1")
    return 1


@pytest.mark.parametrize(
    "case",
    [
        "scoped-hit",
        "scoped-miss",
        "retired-target",
        "unresolved-scope",
        "min-n-gated-aggregate",
        "via-link-to-side",
        "custom-resolver-and-row-visibility",
    ],
)
def test_every_read_surface_is_byte_identical_with_and_without_scope_cache(
    case: str,
    make_registry: RegistryFactory,
    make_policy: PolicyFactory,
    make_store: StoreFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Each case has a literal pin that prevents degenerate identity."""
    registry = _library_registry(make_registry)
    store = make_store(registry)
    _seed_two_shelves(store)
    target_id = "book-target"
    min_n = _seed_byte_identity_case(store, case, target_id)

    rules: dict[str, list[object]] = {
        "Library": [SelfScope(level="library")],
        "Shelf": [
            SelfScope(level="shelf"),
            ViaLink(link_api_name="inLibrary", direction="from", parent_type="Library"),
        ],
        "Book": [ViaLink(link_api_name="onShelf", direction="from", parent_type="Shelf")],
    }
    row_visibility = None
    if case == "via-link-to-side":
        # The book is scoped by the loan it is out on. The governing link runs
        # Loan -> Book, so the book is the link's TO side and the rule reads
        # `links_to` -- `tests/test_scope.py`'s `reservedFor` uses the same
        # shape. Every other case here resolves `direction="from"`, so this is
        # the only case that reaches the cache's `links_to` dispatch; without
        # it, a cache that answered `links_to` with `links_from` stays green.
        rules["Book"] = [
            ViaLink(link_api_name="loanedBook", direction="to", parent_type="Loan")
        ]
        rules["Loan"] = [DirectProperty(level="shelf", property_name="shelf_id")]
    if case == "custom-resolver-and-row-visibility":

        def custom_shelf(author_store: Store, obj_type: str, obj_id: str) -> str | None:
            row = author_store.read_current(obj_type, obj_id)
            return None if row is None else str(row.payload["shelf_id"])

        def linked_book_only(
            row_store: RowVisibilityStore,
            _consumer: Consumer,
            _obj_type: str,
            row: dict[str, Any],
        ) -> bool:
            return bool(row_store.links_from("relatedShelf", str(row["id"])))

        rules["Book"] = [CustomResolver(level="shelf", fn=custom_shelf)]
        row_visibility = {"Book": linked_book_only}

    policy = make_policy(
        levels=LEVELS,
        rules=rules,
        row_visibility=row_visibility or {},
        min_n=min_n,
    )
    query = GuardedQuery(store, registry, policy)
    consumer = _human("shelf", "shelf-1")

    cached = _every_guarded_read_surface(query, consumer, target_id)
    monkeypatch.setattr(query_module, "_ScopeReadCache", lambda: None)
    uncached = _every_guarded_read_surface(query, consumer, target_id)

    assert cached == uncached
    unbounded = json.loads(cached["get_objects_unbounded"])
    if case == "scoped-hit":
        assert [row["payload"]["id"] for row in unbounded["ok"]] == [target_id]
    elif case in {"scoped-miss", "retired-target", "unresolved-scope"}:
        assert unbounded == {"ok": []}
    elif case == "min-n-gated-aggregate":
        assert len(unbounded["ok"]) == 2
        assert json.loads(cached["aggregate"])["code"] == "MIN_N_VIOLATION"
    elif case == "via-link-to-side":
        # Reading the SAME link in the other direction answers `[]` here, so a
        # cache that dispatched `links_from` for a `direction="to"` rule would
        # hide the row -- which is what makes this pin discriminating.
        assert [row["payload"]["id"] for row in unbounded["ok"]] == [target_id]
        assert store.links_from("loanedBook", target_id) == []
    else:
        assert [row["payload"]["id"] for row in unbounded["ok"]] == [target_id]


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
    assert gq.get_objects(consumer, "Book", limit=None) == []


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
    results = gq.get_objects(consumer, "Book", where={"shelf_id": "shelf-1"}, limit=None)
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
    with raises_code(VisibilityError, "VISIBILITY_DENIED"):
        gq.get_objects(human, "Book", limit=None, order_by="acquisition_cost")

    # AI is permitted (acquisition_cost is ai_usable) -- no raise.
    results = gq.get_objects(ai, "Book", where={"acquisition_cost": 42.0}, limit=None)
    assert [r.payload["id"] for r in results] == ["book-1"]
    ordered = gq.get_objects(ai, "Book", limit=None, order_by="acquisition_cost")
    assert [r.payload["id"] for r in ordered] == ["book-1"]


def test_unknown_where_key_is_rejected_before_non_match(
    make_registry: RegistryFactory,
    make_policy: PolicyFactory,
    make_store: StoreFactory,
) -> None:
    registry = _library_registry(make_registry)
    store = make_store(registry)
    _seed_two_shelves(store)
    store.insert("Book", {"id": "book-1", "shelf_id": "shelf-1", "rating": 4.0}, SRC)
    gq = GuardedQuery(store, registry, _library_policy(make_policy))

    with raises_code(ValidationFailed, "UNKNOWN_FIELD") as excinfo:
        gq.get_objects(_human("shelf", "shelf-1"), "Book", where={"ratng": 4.0})
    assert str(excinfo.value) == "Book: where= names unknown field(s) ['ratng']"

    with raises_code(ValidationFailed, "UNKNOWN_FIELD") as excinfo:
        gq.get_objects(
            _human("shelf", "shelf-1"),
            "Book",
            limit=None,
            order_by=("ratng", "desc"),
        )
    assert str(excinfo.value) == ("Book: order_by names unknown field(s) ['ratng']")


# -- min-N ------------------------------------------------------------


@pytest.mark.parametrize("func", ["mean", "count", "sum", "min", "max"])
def test_every_aggregate_function_below_min_n_raises(
    func: str,
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
        gq.aggregate(consumer, "Book", "rating", func=func)
    message = str(excinfo.value)
    assert "2" not in message
    assert "withheld" in message


@pytest.mark.parametrize(
    ("func", "expected"),
    [
        ("mean", 4.0),
        ("count", 3),
        ("sum", 12.0),
        ("min", 3.0),
        ("max", 5.0),
    ],
)
def test_every_aggregate_function_at_min_n_returns_its_reduction(
    func: str,
    expected: int | float,
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
    result = gq.aggregate(consumer, "Book", "rating", func=func)
    assert result == pytest.approx(expected)


def test_aggregate_default_function_remains_mean(
    make_registry: RegistryFactory,
    make_policy: PolicyFactory,
    make_store: StoreFactory,
) -> None:
    registry = _library_registry(make_registry)
    store = make_store(registry)
    _seed_two_shelves(store)
    for index, rating in enumerate((3.0, 4.0, 5.0)):
        store.insert(
            "Book",
            {"id": f"book-{index}", "shelf_id": "shelf-1", "rating": rating},
            SRC,
        )
    gq = GuardedQuery(store, registry, _library_policy(make_policy))

    assert gq.aggregate(_human("shelf", "shelf-1"), "Book", "rating") == pytest.approx(4.0)


@pytest.mark.parametrize("func", [None, "median"])
def test_aggregate_rejects_unsupported_or_none_function(
    func: object,
    make_registry: RegistryFactory,
    make_policy: PolicyFactory,
    make_store: StoreFactory,
) -> None:
    registry = _library_registry(make_registry)
    gq = GuardedQuery(make_store(registry), registry, _library_policy(make_policy))

    with raises_code(ValidationFailed, "INVALID_PARAMS"):
        gq.aggregate(_human("shelf", "shelf-1"), "Book", "rating", func=func)


def test_aggregate_refuses_an_internal_non_float_result(
    make_registry: RegistryFactory,
    make_policy: PolicyFactory,
    make_store: StoreFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = _library_registry(make_registry)
    query = GuardedQuery(make_store(registry), registry, _library_policy(make_policy))
    monkeypatch.setattr(query, "_aggregate", lambda *args, **kwargs: {})

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
    monkeypatch.setattr(query, "_aggregate", lambda *args, **kwargs: 1.0)

    with raises_code(InternalError, "INTERNAL_ERROR"):
        query.aggregate_by(_human("shelf", "shelf-1"), "Book", "rating", "shelf_id")


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
        gq.aggregate_by(_human("shelf", "shelf-1"), "Book", "rating", "shelf_id")

    message = str(excinfo.value)
    assert "3" not in message
    assert "withheld" in message


@pytest.mark.parametrize("func", ["mean", "count", "sum", "min", "max"])
def test_every_grouped_aggregate_function_applies_min_n_per_group(
    func: str,
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
    result = gq.aggregate_by(consumer, "Book", "rating", "shelf_id", func=func)
    expected: int | float = (
        3
        if func == "count"
        else {
            "mean": 4.0,
            "sum": 12.0,
            "min": 4.0,
            "max": 4.0,
        }[func]
    )
    assert result == {"shelf-1": pytest.approx(expected)}


@pytest.mark.parametrize("func", ["mean", "count", "sum", "min", "max"])
def test_grouped_empty_selection_refuses_like_ungrouped_for_every_function(
    func: str,
    make_registry: RegistryFactory,
    make_policy: PolicyFactory,
    make_store: StoreFactory,
) -> None:
    registry = _library_registry(make_registry)
    store = make_store(registry)
    _seed_two_shelves(store)
    gq = GuardedQuery(store, registry, _library_policy(make_policy))
    consumer = _human("shelf", "shelf-1")

    with raises_code(VisibilityError, "MIN_N_VIOLATION") as ungrouped:
        gq.aggregate(consumer, "Book", "rating", func=func)
    with raises_code(VisibilityError, "MIN_N_VIOLATION") as grouped:
        gq.aggregate_by(consumer, "Book", "rating", "shelf_id", func=func)

    assert str(grouped.value) == str(ungrouped.value)


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


# -- reverse traversal -------------------------------------------------------


def test_traverse_forward_and_reverse_are_symmetric_and_scope_filters_sources(
    make_registry: RegistryFactory,
    make_policy: PolicyFactory,
    make_store: StoreFactory,
) -> None:
    registry = _library_registry(make_registry)
    store = make_store(registry)
    store.insert("Library", {"id": "lib-1"}, SRC)
    store.insert("Shelf", {"id": "shelf-1"}, SRC)
    store.insert("Shelf", {"id": "shelf-2"}, SRC)
    store.create_link("inLibrary", "shelf-1", "lib-1")
    store.create_link("inLibrary", "shelf-2", "lib-1")
    store.insert("Book", {"id": "book-a", "shelf_id": "shelf-1"}, SRC)
    store.insert("Book", {"id": "book-c", "shelf_id": "shelf-2"}, SRC)
    store.create_link("relatedShelf", "book-a", "shelf-1")
    store.create_link("relatedShelf", "book-c", "shelf-1")
    store.create_link("relatedShelf", "book-a", "shelf-2")
    gq = GuardedQuery(store, registry, _library_policy(make_policy))

    broad = _human("library", "lib-1")
    forward = gq.traverse(broad, "relatedShelf", "book-a")
    reverse = gq.traverse(broad, "relatedShelf", "shelf-1", reverse=True)
    assert {row.payload["id"] for row in forward} == {"shelf-1", "shelf-2"}
    assert {row.payload["id"] for row in reverse} == {"book-a", "book-c"}

    narrow = _human("shelf", "shelf-1")
    scoped_reverse = gq.traverse(narrow, "relatedShelf", "shelf-1", reverse=True)
    assert [row.payload["id"] for row in scoped_reverse] == ["book-a"]


# -- identity-revealing traversal ------------------------------------------


@pytest.mark.parametrize("reverse", [False, True])
def test_identity_revealing_traverse_denied_before_target_resolution(
    reverse: bool,
    make_registry: RegistryFactory,
    make_policy: PolicyFactory,
    make_store: StoreFactory,
    monkeypatch: pytest.MonkeyPatch,
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

    forbidden_lookup = store.links_to if reverse else store.links_from

    def fail_if_target_lookup_is_attempted(*_args: object, **_kwargs: object) -> object:
        raise AssertionError(f"target lookup occurred through {forbidden_lookup.__name__}")

    monkeypatch.setattr(
        store, "links_to" if reverse else "links_from", fail_if_target_lookup_is_attempted
    )
    with raises_code(VisibilityError, "VISIBILITY_DENIED"):
        gq.traverse(human, "loanedTo", "borrower-1" if reverse else "loan-1", reverse=reverse)

    # The AI branch remains allowed to resolve the link in either direction;
    # restore the store lookup that the gate deliberately skipped for humans.
    monkeypatch.undo()

    # Borrower has no scope rule declared -> its chain is unresolvable ->
    # deny-by-default even for the AI consumer that is allowed to traverse
    # the link itself. Give Borrower an unscoped-type escape hatch instead
    # to prove the AI traversal itself is not blocked by identity_revealing.
    policy = _library_policy(make_policy)
    policy.unscoped_types = policy.unscoped_types | {"Borrower"}
    gq2 = GuardedQuery(store, registry, policy)
    anchor = "borrower-1" if reverse else "loan-1"
    results = gq2.traverse(ai, "loanedTo", anchor, reverse=reverse)
    expected = ["loan-1"] if reverse else ["borrower-1"]
    assert [r.payload["id"] for r in results] == expected


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
    store_public_attrs = {name for name in dir(ObjectStore) if not name.startswith("_")}
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
    guarded_rows = gq.get_objects(consumer, "Book", limit=None)
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
                PropertyDef(name="org_id", type="str", required=False),
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
                PropertyDef(name="org_id", type="str", required=False),
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


ORG = "acme"


def _shared_field_policy(make_policy: PolicyFactory) -> ScopePolicy:
    """Both types route at their OWN narrow level and at a shared broad one.

    The broad `org` rule is what lets a test give a consumer sight of rows its
    narrow scope key would never have matched. That used to be done with
    `policy.unscoped_types.add(...)`, which put the type in `unscoped_types`
    while it still declared `rules` -- a declaration the engine now refuses
    (`tests/test_scope_declaration_integrity.py`), because `unscoped_types`
    killed the scope bound while the `rules` entry kept the hidden scope key
    exempt from the supplied-value `where=` gate.

    Routing at a second, broader level reproduces every precondition those
    tests need -- rows visible despite a hidden narrow key, and that key still
    in `_scope_key_fields` -- while being a legal declaration the authoring
    sugar can produce (`scope=[rule, rule]`). It also models the residual
    `_scope_key_fields` documents more faithfully than the old hack did: the
    exemption applying to "consumers broader than the declaring type's own
    scope level" is exactly what an org-scoped consumer is here.
    """
    return make_policy(
        levels=["person", "group", "org"],
        rules={
            "TeamA": [
                DirectProperty(level="org", property_name="org_id"),
                DirectProperty(level="person", property_name="person_id"),
            ],
            "RecordB": [
                DirectProperty(level="org", property_name="org_id"),
                DirectProperty(level="group", property_name="group_id"),
            ],
        },
        min_n=3,
    )


def _hide_record_b_group_id(registry: OntologyRegistry) -> None:
    """Make RecordB's DirectProperty scope key hidden from humans."""
    obj_def = registry.get_object_type("RecordB")
    group_id = next(prop for prop in obj_def.properties if prop.name == "group_id")
    group_id.sensitivity = Sensitivity(ai_usable=True, human_visible=False)


def test_scope_key_exemption_does_not_pool_across_types(
    make_registry: RegistryFactory,
    make_policy: PolicyFactory,
    make_store: StoreFactory,
) -> None:
    registry = _shared_field_registry(make_registry)
    store = make_store(registry)
    store.insert("TeamA", {"id": "a-1", "person_id": "p-1"}, SRC)
    store.insert("RecordB", {"id": "b-1", "group_id": "p-1", "person_id": "p-1"}, SRC)
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
    results = gq.get_objects(person_consumer, "TeamA", where={"person_id": "p-1"}, limit=None)
    assert [r.payload["id"] for r in results] == ["a-1"]


# -- min-N visibility denial must not echo a sensitivity-hidden group key value


def test_visibility_denial_message_does_not_leak_hidden_group_key_value(
    make_registry: RegistryFactory,
    make_policy: PolicyFactory,
    make_store: StoreFactory,
) -> None:
    registry = _shared_field_registry(make_registry)
    store = make_store(registry)
    # group_id doubles as RecordB's scope-routing DirectProperty AND (in
    # this variant) a sensitivity-hidden field. Grouping is now refused up
    # front, and that visibility-denial message must not reveal its value.
    _hide_record_b_group_id(registry)

    store.insert("RecordB", {"id": "b-1", "group_id": "secret-group", "score": 1.0}, SRC)
    gq = GuardedQuery(store, registry, _shared_field_policy(make_policy))

    consumer = _human("group", "secret-group")
    with raises_code(VisibilityError, "VISIBILITY_DENIED") as excinfo:
        gq.aggregate_by(consumer, "RecordB", "score", "group_id")
    assert "secret-group" not in str(excinfo.value)


@pytest.mark.parametrize(
    ("order_by", "leaked_order"),
    [
        ("group_id", ["r1", "r2", "r0"]),
        (("group_id", "desc"), ["r0", "r2", "r1"]),
    ],
    ids=["string", "pair"],
)
def test_order_by_hidden_scope_key_denied_before_rank_is_observable(
    order_by: str | tuple[str, str],
    leaked_order: list[str],
    make_registry: RegistryFactory,
    make_policy: PolicyFactory,
    make_store: StoreFactory,
) -> None:
    registry = _shared_field_registry(make_registry)
    _hide_record_b_group_id(registry)
    store = make_store(registry)
    for object_id, group_id in [
        ("r0", "zulu-group"),
        ("r1", "alpha-group"),
        ("r2", "middle-group"),
    ]:
        store.insert(
            "RecordB",
            {"id": object_id, "org_id": ORG, "group_id": group_id, "score": 1.0},
            SRC,
        )
    policy = _shared_field_policy(make_policy)
    gq = GuardedQuery(store, registry, policy)

    # Broad-scoped, so every seeded row is in view and removing the order gate
    # exposes the true hidden-field rank rather than a constant one-group
    # ordering. The rows' own `group_id` is still hidden from this consumer.
    consumer = _human("org", ORG)
    observed_order: list[str] | None = None
    denial: VisibilityError | None = None
    try:
        rows = gq.get_objects(consumer, "RecordB", limit=None, order_by=order_by)
        observed_order = [str(row.payload["id"]) for row in rows]
    except VisibilityError as exc:
        denial = exc

    assert observed_order != leaked_order
    assert denial is not None
    assert denial.code == "VISIBILITY_DENIED"


def test_aggregate_by_hidden_scope_key_denied_without_returning_group_keys(
    make_registry: RegistryFactory,
    make_policy: PolicyFactory,
    make_store: StoreFactory,
) -> None:
    registry = _shared_field_registry(make_registry)
    _hide_record_b_group_id(registry)
    store = make_store(registry)
    for i in range(3):
        store.insert(
            "RecordB",
            {
                "id": f"b-{i}",
                "group_id": "secret-group",
                "score": float(i + 1),
            },
            SRC,
        )
    gq = GuardedQuery(store, registry, _shared_field_policy(make_policy))

    consumer = _human("group", "secret-group")
    returned_groups: dict[str, int | float] | None = None
    denial: VisibilityError | None = None
    try:
        returned_groups = gq.aggregate_by(consumer, "RecordB", "score", "group_id")
    except VisibilityError as exc:
        denial = exc

    assert returned_groups is None or "secret-group" not in returned_groups
    assert denial is not None
    assert denial.code == "VISIBILITY_DENIED"


def test_get_objects_redacts_hidden_scope_key_from_returned_payload(
    make_registry: RegistryFactory,
    make_policy: PolicyFactory,
    make_store: StoreFactory,
) -> None:
    registry = _shared_field_registry(make_registry)
    _hide_record_b_group_id(registry)
    store = make_store(registry)
    store.insert("RecordB", {"id": "b-1", "group_id": "secret-group"}, SRC)
    gq = GuardedQuery(store, registry, _shared_field_policy(make_policy))

    rows = gq.get_objects(_human("group", "secret-group"), "RecordB", limit=None)

    assert [row.payload for row in rows] == [{"id": "b-1"}]
    assert all("group_id" not in row.payload for row in rows)


def test_contains_alphabet_walk_cannot_recover_hidden_scope_key(
    make_registry: RegistryFactory,
    make_policy: PolicyFactory,
    make_store: StoreFactory,
) -> None:
    registry = _shared_field_registry(make_registry)
    _hide_record_b_group_id(registry)
    store = make_store(registry)
    hidden_value = "amy-4402"
    store.insert("RecordB", {"id": "target", "org_id": ORG, "group_id": hidden_value}, SRC)
    policy = _shared_field_policy(make_policy)
    gq = GuardedQuery(store, registry, policy)
    # Broad-scoped, so the target is in view independently of its hidden scope
    # key. With the gate removed, returned row ids become a substring-
    # membership oracle -- verified live: mutating `_hidden_fields` open lets
    # this same walk recover `amy-4402`.
    consumer = _human("org", ORG)

    assert [
        row.payload["id"]
        for row in gq.get_objects(
            consumer,
            "RecordB",
            where={"id": {"contains": "targ"}},
            limit=None,
        )
    ] == ["target"]

    alphabet = "abcdefghijklmnopqrstuvwxyz0123456789-"
    matching_fragments = [""]
    for _ in range(len(hidden_value)):
        next_fragments: list[str] = []
        for prefix in matching_fragments:
            for character in alphabet:
                candidate = prefix + character
                try:
                    rows = gq.get_objects(
                        consumer,
                        "RecordB",
                        where={"group_id": {"contains": candidate}},
                        limit=None,
                    )
                except VisibilityError as exc:
                    assert exc.code == "VISIBILITY_DENIED"
                    continue
                if any(row.payload["id"] == "target" for row in rows):
                    next_fragments.append(candidate)
        matching_fragments = next_fragments

    recovered_values = set(matching_fragments)
    assert hidden_value not in recovered_values


# -- finding 3: one snapshot, so the gate and the matcher cannot disagree ---


class _TwoFacedMapping(dict):
    """A mapping that answers `items()` differently on each read.

    Not a hypothetical: any caller-supplied object reaching a public read is
    the caller's to define, and `where` was read once by the field gate and
    again by the matcher. This is the smallest thing that tells those two
    reads apart.
    """

    def __init__(self, *faces):
        super().__init__(faces[0])
        self._faces = faces
        self.reads = 0

    def items(self):
        face = self._faces[min(self.reads, len(self._faces) - 1)]
        self.reads += 1
        return face.items()


def _walkable_record_b(make_registry, make_policy, make_store, hidden_value):
    """RecordB with a hidden scope key, kept in view independently of it."""
    registry = _shared_field_registry(make_registry)
    _hide_record_b_group_id(registry)
    store = make_store(registry)
    store.insert("RecordB", {"id": "target", "org_id": ORG, "group_id": hidden_value}, SRC)
    policy = _shared_field_policy(make_policy)
    return GuardedQuery(store, registry, policy), _human("org", ORG)


def test_two_faced_where_mapping_cannot_split_the_gate_from_the_matcher(
    make_registry: RegistryFactory,
    make_policy: PolicyFactory,
    make_store: StoreFactory,
) -> None:
    """The classified predicate must be the executed predicate.

    Face one is supply-shaped equality on a value that matches nothing, so
    the scope-key exemption lets it through. Face two is the `contains`
    alphabet-walk step that `test_contains_alphabet_walk_cannot_recover_hidden_scope_key`
    pins as unrecoverable. Reading `where` twice handed the gate face one and
    the matcher face two, running the walk under the exemption's authority.
    """
    gq, consumer = _walkable_record_b(make_registry, make_policy, make_store, "amy-4402")
    where = _TwoFacedMapping(
        {"group_id": "no-such-value"},
        {"group_id": {"contains": "a"}},
    )

    rows = gq.get_objects(consumer, "RecordB", where=where, limit=None)

    # Face one ran, and it matches nothing. The walk step never executed.
    assert rows == []


def test_two_faced_where_mapping_is_gated_on_the_face_it_shows_first(
    make_registry: RegistryFactory,
    make_policy: PolicyFactory,
    make_store: StoreFactory,
) -> None:
    """The other ordering, so the caller cannot choose which read is gated.

    Showing the learning-shaped face first must refuse, exactly as that face
    would on its own -- otherwise a second face could launder it.
    """
    gq, consumer = _walkable_record_b(make_registry, make_policy, make_store, "amy-4402")
    where = _TwoFacedMapping(
        {"group_id": {"contains": "a"}},
        {"group_id": "no-such-value"},
    )

    with raises_code(VisibilityError, "VISIBILITY_DENIED"):
        gq.get_objects(consumer, "RecordB", where=where, limit=None)


def test_two_faced_condition_cannot_split_the_gate_from_the_matcher(
    make_registry: RegistryFactory,
    make_policy: PolicyFactory,
    make_store: StoreFactory,
) -> None:
    """The same split one level down, in the CONDITION rather than the
    `where` mapping -- the classifier and the compiler each re-read this
    object too, so it gets its own name."""
    gq, consumer = _walkable_record_b(make_registry, make_policy, make_store, "amy-4402")
    condition = _TwoFacedMapping(
        {"in": ["no-such-value"]},
        {"contains": "a"},
    )

    rows = gq.get_objects(consumer, "RecordB", where={"group_id": condition}, limit=None)

    assert rows == []


def test_two_faced_where_mapping_is_read_exactly_once(
    make_registry: RegistryFactory,
    make_policy: PolicyFactory,
    make_store: StoreFactory,
) -> None:
    """States the fix directly, so a future path that re-reads the caller's
    mapping reds here even if it happens to classify consistently."""
    gq, consumer = _walkable_record_b(make_registry, make_policy, make_store, "amy-4402")
    where = _TwoFacedMapping({"group_id": "amy-4402"})

    gq.get_objects(consumer, "RecordB", where=where, limit=None)

    assert where.reads == 1


# -- finding 6: an operand that supplies no value is not supply-shaped -----


def test_bare_null_is_not_supply_shaped_on_a_hidden_scope_key(
    make_registry: RegistryFactory,
    make_policy: PolicyFactory,
    make_store: StoreFactory,
) -> None:
    """`where={"group_id": None}` needs no prior knowledge, so it cannot ride
    the exemption whose whole premise is that the caller already holds the
    value being named. It was a per-row null probe on a hidden field.
    """
    registry = _shared_field_registry(make_registry)
    _hide_record_b_group_id(registry)
    store = make_store(registry)
    store.insert(
        "RecordB",
        {"id": "keyed", "org_id": ORG, "group_id": "secret-group"},
        SRC,
    )
    store.insert("RecordB", {"id": "unkeyed", "org_id": ORG}, SRC)
    policy = _shared_field_policy(make_policy)
    gq = GuardedQuery(store, registry, policy)
    consumer = _human("org", ORG)

    with raises_code(VisibilityError, "VISIBILITY_DENIED"):
        gq.exists(consumer, "RecordB", where={"id": "unkeyed", "group_id": None})


def test_a_wrongly_typed_bare_operand_never_preempts_the_visibility_denial(
    make_registry: RegistryFactory,
    make_policy: PolicyFactory,
    make_store: StoreFactory,
) -> None:
    """Type-checking the bare operand must not become a hidden-field oracle.

    `_compile_where` runs only after the unknown-field and hidden-field
    gates, and the new `eq` check lives inside it. So a hidden field answers
    `VISIBILITY_DENIED` whatever the operand's type, exactly as before --
    measured on both a correct and a wrongly typed operand, so a caller
    cannot tell a hidden field's declared type apart from its existence.

    On the narrow scope-key EXEMPTION the refusal does now surface, because
    the exemption lets that one shape reach compilation: `{"group_id": 1}`
    on a `str` key answers `OPERATOR_TYPE_MISMATCH` where it used to match
    nothing silently. That is not an oracle -- the verdict is a function of
    the DECLARED type, which the caller already reads off the ontology, and
    of no row's value.
    """
    registry = _shared_field_registry(make_registry)
    _hide_record_b_group_id(registry)
    store = make_store(registry)
    store.insert("RecordB", {"id": "keyed", "org_id": ORG, "group_id": "secret-group"}, SRC)
    policy = _shared_field_policy(make_policy)
    gq = GuardedQuery(store, registry, policy)
    consumer = _human("org", ORG)

    # A non-exempt shape stays a uniform denial, right operand type or wrong.
    with raises_code(VisibilityError, "VISIBILITY_DENIED"):
        gq.get_objects(consumer, "RecordB", where={"group_id": {"ne": "secret-group"}})
    with raises_code(VisibilityError, "VISIBILITY_DENIED"):
        gq.get_objects(consumer, "RecordB", where={"group_id": {"ne": 1}})

    # The exemption admits the bare shape, so the type check is reached.
    with raises_code(ValidationFailed, "OPERATOR_TYPE_MISMATCH"):
        gq.get_objects(consumer, "RecordB", where={"group_id": 1})


def test_in_over_a_list_containing_null_is_not_supply_shaped(
    make_registry: RegistryFactory,
    make_policy: PolicyFactory,
    make_store: StoreFactory,
) -> None:
    """The same probe through the `in` door, which keeps its exemption only
    for a non-empty list of actual values."""
    gq, consumer = _walkable_record_b(make_registry, make_policy, make_store, "amy-4402")

    with raises_code(VisibilityError, "VISIBILITY_DENIED"):
        gq.get_objects(consumer, "RecordB", where={"group_id": {"in": [None]}}, limit=None)
    with raises_code(VisibilityError, "VISIBILITY_DENIED"):
        gq.get_objects(consumer, "RecordB", where={"group_id": {"in": []}}, limit=None)


def test_supplied_scalar_keeps_the_scope_key_exemption(
    make_registry: RegistryFactory,
    make_policy: PolicyFactory,
    make_store: StoreFactory,
) -> None:
    """The release direction spec §2 deliberately keeps: naming a value you
    already hold stays exempt, through bare equality and through `in` over a
    list of values. A mutation that classifies every shape as learning-shaped
    reds here rather than passing as "more secure"."""
    gq, consumer = _walkable_record_b(make_registry, make_policy, make_store, "amy-4402")

    assert [
        row.payload["id"]
        for row in gq.get_objects(consumer, "RecordB", where={"group_id": "amy-4402"}, limit=None)
    ] == ["target"]
    assert [
        row.payload["id"]
        for row in gq.get_objects(
            consumer,
            "RecordB",
            where={"group_id": {"in": ["amy-4402", "other"]}},
            limit=None,
        )
    ] == ["target"]


def test_bare_null_still_filters_a_field_that_is_not_hidden(
    make_registry: RegistryFactory,
    make_policy: PolicyFactory,
    make_store: StoreFactory,
) -> None:
    """Null filtering is a real query need and stays available wherever the
    field is readable -- the tightening is scoped to the hidden set."""
    registry = _shared_field_registry(make_registry)
    store = make_store(registry)
    store.insert("TeamA", {"id": "a-1", "org_id": ORG, "person_id": "p-1"}, SRC)
    store.insert("TeamA", {"id": "a-2", "org_id": ORG}, SRC)
    policy = _shared_field_policy(make_policy)
    gq = GuardedQuery(store, registry, policy)

    # Broad-scoped so the un-keyed row is in view at all: a person-scoped
    # consumer cannot resolve a row that carries no `person_id`.
    rows = gq.get_objects(_human("org", ORG), "TeamA", where={"person_id": None}, limit=None)

    assert [row.payload["id"] for row in rows] == ["a-2"]


@pytest.mark.parametrize("operator", ["gt", "gte", "lt", "lte", "ne"])
def test_learning_shaped_where_operator_refused_on_hidden_scope_key(
    operator: str,
    make_registry: RegistryFactory,
    make_policy: PolicyFactory,
    make_store: StoreFactory,
) -> None:
    registry = _shared_field_registry(make_registry)
    _hide_record_b_group_id(registry)
    store = make_store(registry)
    for i in range(3):
        store.insert(
            "RecordB",
            {
                "id": f"b-{i + 1}",
                "group_id": "secret-group",
                "score": float(i + 1),
            },
            SRC,
        )
    gq = GuardedQuery(store, registry, _shared_field_policy(make_policy))
    consumer = _human("group", "secret-group")
    where = {"group_id": {operator: "secret-group"}}

    with raises_code(VisibilityError, "VISIBILITY_DENIED"):
        gq.get_objects(consumer, "RecordB", where=where, limit=None)
    with raises_code(VisibilityError, "VISIBILITY_DENIED"):
        gq.aggregate(consumer, "RecordB", "score", where=where)
    with raises_code(VisibilityError, "VISIBILITY_DENIED"):
        gq.count_contributors(consumer, "RecordB", where=where)


def test_in_non_list_where_refused_on_hidden_scope_key(
    make_registry: RegistryFactory,
    make_policy: PolicyFactory,
    make_store: StoreFactory,
) -> None:
    registry = _shared_field_registry(make_registry)
    _hide_record_b_group_id(registry)
    store = make_store(registry)
    store.insert(
        "RecordB",
        {"id": "b-1", "group_id": "secret-group", "score": 1.0},
        SRC,
    )
    gq = GuardedQuery(store, registry, _shared_field_policy(make_policy))
    consumer = _human("group", "secret-group")
    where = {"group_id": {"in": ("secret-group",)}}

    with raises_code(VisibilityError, "VISIBILITY_DENIED"):
        gq.get_objects(consumer, "RecordB", where=where, limit=None)
    with raises_code(VisibilityError, "VISIBILITY_DENIED"):
        gq.aggregate(consumer, "RecordB", "score", where=where)


@pytest.mark.parametrize(
    "condition",
    [
        {"bogus": "secret-group"},
        {},
        {"gt": "a", "lt": "z"},
    ],
    ids=["unknown-operator", "empty-dict", "multi-key-dict"],
)
def test_hidden_scope_key_bogus_operator_denied_before_operator_validation(
    condition: dict[str, str],
    make_registry: RegistryFactory,
    make_policy: PolicyFactory,
    make_store: StoreFactory,
) -> None:
    registry = _shared_field_registry(make_registry)
    _hide_record_b_group_id(registry)
    store = make_store(registry)
    store.insert(
        "RecordB",
        {"id": "b-1", "group_id": "secret-group", "score": 1.0},
        SRC,
    )
    gq = GuardedQuery(store, registry, _shared_field_policy(make_policy))
    consumer = _human("group", "secret-group")
    where = {"group_id": condition}

    with raises_code(VisibilityError, "VISIBILITY_DENIED"):
        gq.get_objects(consumer, "RecordB", where=where, limit=None)
    with raises_code(VisibilityError, "VISIBILITY_DENIED"):
        gq.aggregate(consumer, "RecordB", "score", where=where)


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
    assert [r.payload["code"] for r in gq.get_objects(consumer, "Item", limit=None)] == ["item-1"]

    # traverse
    assert [r.payload["code"] for r in gq.traverse(consumer, "itemInRegion", "item-1")] == ["reg-1"]

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


def _contributor_registry(
    make_registry: RegistryFactory, *, owned: bool = False
) -> OntologyRegistry:
    """The Reader/Reading pair. `owned=True` declares Reader and byReader
    ontology-owned, the author declaration an Action needs before it may
    retire a Reader and cascade its links."""
    registry = _library_registry(make_registry)
    registry.register_object_type(
        ObjectTypeDef(
            api_name="Reader",
            display_name="Reader",
            description="A person who submits Readings",
            layer="L0",
            properties=[PropertyDef(name="id", type="str")],
            primary_key="id",
            owned=owned,
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
                PropertyDef(name="cohort", type="str", required=False),
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
            owned=owned,
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


# Each shape is a sequence of reads whose released values can be combined by
# an attacker. Keeping the attacks as data makes the disclosure invariant grow
# by adding a shape, rather than by copying another one-off assertion.
DISCLOSURE_REFERENCE_SHAPES: tuple[tuple[str, tuple[dict[str, Any], ...]], ...] = (
    ("hidden-min", ({"population": "all", "func": "min"},)),
    ("hidden-max", ({"population": "all", "func": "max"},)),
    (
        "hidden-sum",
        ({"population": "all", "func": "sum"},),
    ),
    (
        "mean-count-differencing",
        (
            {"population": "all", "func": "mean"},
            {"population": "all", "func": "count"},
            {
                "population": "without-r0",
                "func": "mean",
                "where": {"id": {"ne": "r0"}},
            },
            {
                "population": "without-r0",
                "func": "count",
                "where": {"id": {"ne": "r0"}},
            },
        ),
    ),
    (
        "group-by-steering",
        (
            {
                "population": "cohorts",
                "func": "mean",
                "group_by": "cohort",
            },
        ),
    ),
)

DISCLOSURE_SEEDED_VALUES: dict[str, tuple[float, ...]] = {
    # The complete sum is 20.0, another row's distinct hidden value. This
    # makes the sum shape catch a sum-release mutation without relying on
    # D4's separate narrowing refusal.
    "hidden-sum": (1.0, 4.0, 7.0, 12.0, 20.0, -24.0),
}


@pytest.mark.parametrize(
    ("backend_name", "store_type"),
    (("sqlite", ObjectStore), ("memory", InMemoryStore)),
    ids=("sqlite", "memory"),
)
@pytest.mark.parametrize(
    ("shape_name", "reads"),
    DISCLOSURE_REFERENCE_SHAPES,
    ids=[shape_name for shape_name, _ in DISCLOSURE_REFERENCE_SHAPES],
)
def test_released_reads_cannot_recover_an_individual_hidden_value(
    backend_name: str,
    store_type: type[Store],
    shape_name: str,
    reads: tuple[dict[str, Any], ...],
    make_registry: RegistryFactory,
    make_policy: PolicyFactory,
) -> None:
    """Any sequence of released reads preserves individual-value secrecy.

    Every generated population first releases the same operation over the
    visible ``score`` field. That positive check is the anti-vacuity guard: a
    typo, empty selection, or under-threshold group cannot make the invariant
    pass merely because there was nothing to attack.
    """
    del backend_name
    registry = _contributor_registry(make_registry)
    store = store_type(registry)
    _seed_two_shelves(store)
    values = DISCLOSURE_SEEDED_VALUES.get(shape_name, (1.0, 4.0, 7.0, 12.0, 20.0, 31.0))
    seeded = tuple((f"r{i}", value, "a" if i < 3 else "b") for i, value in enumerate(values))
    for row_id, value, cohort in seeded:
        reader_id = f"reader-{row_id}"
        store.insert("Reader", {"id": reader_id}, SRC)
        store.insert(
            "Reading",
            {
                "id": row_id,
                "shelf_id": "shelf-1",
                "score": value,
                "cohort": cohort,
                "raw_score": value,
            },
            SRC,
        )
        store.create_link("byReader", row_id, reader_id)

    hidden_values = tuple(
        float(row.payload["raw_score"])
        for row in store.read_all("Reading")
        if "raw_score" in row.payload
    )
    assert len(hidden_values) == len(seeded)
    assert len(set(hidden_values)) == len(hidden_values)

    query = GuardedQuery(store, registry, _contributor_policy(make_policy))
    human = _human("shelf", "shelf-1")
    released: dict[tuple[str, str], list[float]] = {}

    for read in reads:
        where = read.get("where")
        group_by = read.get("group_by")
        func = read["func"]

        if group_by is None:
            visible_probe = query.aggregate(human, "Reading", "score", where=where, func=func)
            assert isinstance(visible_probe, (int, float))
        else:
            visible_probe = query.aggregate_by(
                human,
                "Reading",
                "score",
                group_by,
                where=where,
                func=func,
            )
            assert visible_probe
            assert all(isinstance(value, (int, float)) for value in visible_probe.values())

        try:
            if group_by is None:
                hidden_result = query.aggregate(
                    human,
                    "Reading",
                    "raw_score",
                    where=where,
                    func=func,
                    _author_dispatch=_AUTHOR_DISPATCH,
                )
            else:
                hidden_result = query.aggregate_by(
                    human,
                    "Reading",
                    "raw_score",
                    group_by,
                    where=where,
                    func=func,
                    _author_dispatch=_AUTHOR_DISPATCH,
                )
        except VisibilityError as exc:
            assert exc.code == "VISIBILITY_DENIED"
            continue

        values = (
            [float(value) for value in hidden_result.values()]
            if isinstance(hidden_result, dict)
            else [float(hidden_result)]
        )
        released[(read["population"], func)] = values

    candidates = [value for values in released.values() for value in values]
    totals: list[float] = []
    populations = {population for population, _func in released}
    for population in populations:
        sums = released.get((population, "sum"))
        means = released.get((population, "mean"))
        counts = released.get((population, "count"))
        if sums is not None:
            totals.extend(sums)
        if means is not None and counts is not None:
            totals.extend(mean * count for mean, count in zip(means, counts, strict=True))
    candidates.extend(totals)
    candidates.extend(left - right for left in totals for right in totals if left != right)

    assert not any(
        candidate == pytest.approx(hidden_value)
        for candidate in candidates
        for hidden_value in hidden_values
    )


class _DisclosureGateReached(Exception):
    pass


def test_every_public_read_routes_through_the_disclosure_gate(
    monkeypatch: pytest.MonkeyPatch,
    make_registry: RegistryFactory,
    make_policy: PolicyFactory,
    make_store: StoreFactory,
) -> None:
    """A new public read must be listed here and must reach the central gate."""
    registry = _contributor_registry(make_registry)
    query = GuardedQuery(make_store(registry), registry, _contributor_policy(make_policy))
    human = _human("shelf", "shelf-1")
    invokers: dict[str, Callable[[GuardedQuery], object]] = {
        "get_objects": lambda guarded: guarded.get_objects(human, "Reading", limit=None),
        "get_object": lambda guarded: guarded.get_object(human, "Reading", "r0"),
        "count": lambda guarded: guarded.count(human, "Reading"),
        "exists": lambda guarded: guarded.exists(human, "Reading"),
        "traverse": lambda guarded: guarded.traverse(human, "inLibrary", "shelf-1"),
        "aggregate": lambda guarded: guarded.aggregate(human, "Reading", "score"),
        "aggregate_by": lambda guarded: guarded.aggregate_by(human, "Reading", "score", "shelf_id"),
        "count_contributors": lambda guarded: guarded.count_contributors(human, "Reading"),
    }
    public_reads = {
        name
        for name, member in GuardedQuery.__dict__.items()
        if not name.startswith("_") and callable(member)
    }
    assert public_reads == set(invokers)

    def stop_at_disclosure_gate(*_args: object, **_kwargs: object) -> None:
        raise _DisclosureGateReached

    monkeypatch.setattr(GuardedQuery, "_require_coherent_scope", stop_at_disclosure_gate)
    for invoke in invokers.values():
        with pytest.raises(_DisclosureGateReached):
            invoke(query)


def test_count_only_redaction_switch_stays_private_to_guarded_query(
    make_registry: RegistryFactory,
    make_policy: PolicyFactory,
    make_store: StoreFactory,
) -> None:
    """The private read switches cannot escape the guarded engine.

    A future ``**kwargs`` convenience parameter on any typed read could let a
    caller forward ``_redact_rows=False`` and receive raw ``StoredObject``
    payloads, or forward ``_stop_after`` and silently receive a TRUNCATED
    result set -- data loss rather than over-disclosure. The signature check
    catches that structural drift; the calls below prove the three list entry
    points reject each keyword at runtime.
    """
    registry = _library_registry(make_registry)
    store = make_store(registry)
    _seed_two_shelves(store)
    store.insert(
        "Book",
        {
            "id": "book-hidden",
            "shelf_id": "shelf-1",
            "acquisition_cost": 42.0,
        },
        SRC,
    )
    policy = _library_policy(make_policy)
    query = GuardedQuery(store, registry, policy)
    consumer = _human("shelf", "shelf-1")
    ontology = OntologyDef(
        name="library",
        registry=registry,
        policy=policy,
    )
    ontology.validate()
    client = OntologyClient(ontology, store, consumer)
    bound = BoundQuery(query, consumer, registry)

    read_method_names = (
        "get",
        "list",
        "traverse",
        "aggregate",
        "aggregate_by",
        "count",
        "exists",
        "count_contributors",
    )
    for facade in (OntologyClient, BoundQuery):
        for method_name in read_method_names:
            signature = inspect.signature(getattr(facade, method_name))
            assert not any(
                parameter.kind is inspect.Parameter.VAR_KEYWORD
                for parameter in signature.parameters.values()
            ), f"{facade.__name__}.{method_name} must not accept **kwargs"

    helper_signature = inspect.signature(_typed_api.list_objects)
    assert not any(
        parameter.kind is inspect.Parameter.VAR_KEYWORD
        for parameter in helper_signature.parameters.values()
    ), "_typed_api.list_objects must not accept **kwargs"

    rejecting_calls: tuple[tuple[str, str, Callable[[], object]], ...] = (
        (
            "_typed_api.list_objects",
            "_redact_rows",
            lambda: _typed_api.list_objects(
                query,
                consumer,
                "Book",
                None,
                api_name_for=lambda _cls: "Book",
                validate_where_keys=lambda _cls, _where: None,
                _redact_rows=False,  # type: ignore[call-arg]
            ),
        ),
        (
            "OntologyClient.list",
            "_redact_rows",
            lambda: client.list(
                "Book",
                limit=None,
                _redact_rows=False,  # type: ignore[call-arg]
            ),
        ),
        (
            "BoundQuery.list",
            "_redact_rows",
            lambda: bound.list(
                "Book",
                limit=None,
                _redact_rows=False,  # type: ignore[call-arg]
            ),
        ),
        (
            "_typed_api.list_objects",
            "_stop_after",
            lambda: _typed_api.list_objects(
                query,
                consumer,
                "Book",
                None,
                api_name_for=lambda _cls: "Book",
                validate_where_keys=lambda _cls, _where: None,
                _stop_after=1,  # type: ignore[call-arg]
            ),
        ),
        (
            "OntologyClient.list",
            "_stop_after",
            lambda: client.list(
                "Book",
                limit=None,
                _stop_after=1,  # type: ignore[call-arg]
            ),
        ),
        (
            "BoundQuery.list",
            "_stop_after",
            lambda: bound.list(
                "Book",
                limit=None,
                _stop_after=1,  # type: ignore[call-arg]
            ),
        ),
    )
    for _surface, private_keyword, invoke in rejecting_calls:
        with pytest.raises(TypeError, match=private_keyword):
            invoke()

    rows = query.get_objects(consumer, "Book", limit=None)
    assert len(rows) == 1
    assert "acquisition_cost" not in rows[0].payload


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
        store.insert("Reading", {"id": f"r{i}", "shelf_id": "shelf-1", "score": 4.0}, SRC)
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


def test_aggregate_unresolved_contributors_count_as_one_unknown(
    make_registry: RegistryFactory,
    make_policy: PolicyFactory,
    make_store: StoreFactory,
) -> None:
    """Rows whose contributor does not resolve contribute ONE unknown
    identity between them, not one apiece.

    DEVIATION, recorded: this replaces
    `test_aggregate_contributor_unresolved_row_falls_back_to_row_count`,
    which pinned the opposite (each unresolved row counted as its own
    contributor, so 1 Reader + 2 unresolved rows released at min_n=3). That
    fallback mirrored the prototype, but it let the floor be cleared by rows
    the engine has NO evidence came from different people -- and
    `close_link`/`retire` turn resolvable rows into unresolved ones, so a
    correctly-refused selection became releasable. Unresolved is unknown,
    and `covers_scope` already denies on an unresolved level rather than
    failing open; this aligns the contributor count with that rule.

    Never silently dropped is preserved: the unknown population still
    contributes, but only as the single identity the engine can prove.
    """
    registry = _contributor_registry(make_registry)
    store = make_store(registry)
    _seed_two_shelves(store)
    store.insert("Reader", {"id": "reader-1"}, SRC)
    store.insert("Reading", {"id": "r1", "shelf_id": "shelf-1", "score": 4.0}, SRC)
    store.create_link("byReader", "r1", "reader-1")
    # r2, r3 have no byReader link: unresolved, and indistinguishable from
    # each other -- together they are one unknown contributor, so the floor
    # sees 2 (reader-1 + unknown), not 3.
    store.insert("Reading", {"id": "r2", "shelf_id": "shelf-1", "score": 5.0}, SRC)
    store.insert("Reading", {"id": "r3", "shelf_id": "shelf-1", "score": 3.0}, SRC)
    gq = GuardedQuery(store, registry, _contributor_policy(make_policy))

    consumer = _human("shelf", "shelf-1")
    with raises_code(VisibilityError, "MIN_N_VIOLATION"):
        gq.aggregate(consumer, "Reading", "score")
    with raises_code(VisibilityError, "MIN_N_VIOLATION"):
        gq.count_contributors(consumer, "Reading")


def test_aggregate_unresolved_contributor_still_counts_toward_the_floor(
    make_registry: RegistryFactory,
    make_policy: PolicyFactory,
    make_store: StoreFactory,
) -> None:
    """The other direction of the same rule, so a mutation that DROPS
    unresolved rows entirely cannot pass.

    2 distinct Readers + 1 unresolved row clears min_n=3 exactly because the
    unknown population contributes its one identity. Dropping unresolved
    rows would count 2 and refuse; counting them per-row would count 3 here
    too, which is why the test above -- where per-row counting and
    collective counting DIVERGE -- is the one that pins the release
    direction.
    """
    registry = _contributor_registry(make_registry)
    store = make_store(registry)
    _seed_two_shelves(store)
    for i in range(2):
        store.insert("Reader", {"id": f"reader-{i}"}, SRC)
        store.insert("Reading", {"id": f"r{i}", "shelf_id": "shelf-1", "score": 4.0}, SRC)
        store.create_link("byReader", f"r{i}", f"reader-{i}")
    store.insert("Reading", {"id": "r-anon", "shelf_id": "shelf-1", "score": 4.0}, SRC)
    gq = GuardedQuery(store, registry, _contributor_policy(make_policy))

    consumer = _human("shelf", "shelf-1")
    assert gq.aggregate(consumer, "Reading", "score") == pytest.approx(4.0)
    assert gq.count_contributors(consumer, "Reading") == 3


# -- finding 1: the floor must count the population that carried a value ----


def test_aggregate_min_n_counts_only_contributors_of_rows_carrying_the_value(
    make_registry: RegistryFactory,
    make_policy: PolicyFactory,
    make_store: StoreFactory,
) -> None:
    """min-N counted `group_rows`, but `mean` averages only the rows where
    `value_field` is PRESENT -- so 3 rows from 3 Readers cleared the floor
    while the released number came from one of them, and the "mean" was that
    Reader's exact hidden value.

    The floor must be measured over the same population the released number
    is computed from.
    """
    registry = _contributor_registry(make_registry)
    store = make_store(registry)
    _seed_two_shelves(store)
    for i in range(3):
        store.insert("Reader", {"id": f"reader-{i}"}, SRC)
        payload = {"id": f"r{i}", "shelf_id": "shelf-1", "score": 4.0}
        if i == 0:
            # Only reader-0 answered the sensitive question.
            payload["raw_score"] = 42.0
        store.insert("Reading", payload, SRC)
        store.create_link("byReader", f"r{i}", f"reader-{i}")
    gq = GuardedQuery(store, registry, _contributor_policy(make_policy))

    consumer = _human("shelf", "shelf-1")
    with raises_code(VisibilityError, "MIN_N_VIOLATION"):
        gq.aggregate(consumer, "Reading", "raw_score", _author_dispatch=_AUTHOR_DISPATCH)


def test_aggregate_count_func_min_n_counts_only_rows_carrying_the_value(
    make_registry: RegistryFactory,
    make_policy: PolicyFactory,
    make_store: StoreFactory,
) -> None:
    """The `func="count"` face of the same defect, pinned separately.

    `aggregate(func="count")` returned 1 against `count()`'s 3 -- the
    difference IS the number of people who answered, released for a field
    the consumer cannot read. A fix that only guarded `mean` leaves this
    open, so it reds on its own name.
    """
    registry = _contributor_registry(make_registry)
    store = make_store(registry)
    _seed_two_shelves(store)
    for i in range(3):
        store.insert("Reader", {"id": f"reader-{i}"}, SRC)
        payload = {"id": f"r{i}", "shelf_id": "shelf-1", "score": 4.0}
        if i == 0:
            payload["raw_score"] = 42.0
        store.insert("Reading", payload, SRC)
        store.create_link("byReader", f"r{i}", f"reader-{i}")
    gq = GuardedQuery(store, registry, _contributor_policy(make_policy))

    consumer = _human("shelf", "shelf-1")
    assert gq.count(consumer, "Reading") == 3
    with raises_code(VisibilityError, "MIN_N_VIOLATION"):
        gq.aggregate(
            consumer, "Reading", "raw_score", func="count", _author_dispatch=_AUTHOR_DISPATCH
        )


def test_aggregate_releases_when_enough_contributors_carried_the_value(
    make_registry: RegistryFactory,
    make_policy: PolicyFactory,
    make_store: StoreFactory,
) -> None:
    """The release direction: a row missing the value narrows the counted
    population without blocking a selection that still clears the floor.

    4 Readers, 3 of whom answered -- the floor sees those 3 and releases.
    Pins that the fix counts value-carrying rows, not "every row must carry
    the value".
    """
    registry = _contributor_registry(make_registry)
    store = make_store(registry)
    _seed_two_shelves(store)
    for i in range(4):
        store.insert("Reader", {"id": f"reader-{i}"}, SRC)
        payload = {"id": f"r{i}", "shelf_id": "shelf-1", "score": 4.0}
        if i < 3:
            payload["raw_score"] = 6.0
        store.insert("Reading", payload, SRC)
        store.create_link("byReader", f"r{i}", f"reader-{i}")
    gq = GuardedQuery(store, registry, _contributor_policy(make_policy))

    consumer = _human("shelf", "shelf-1")
    assert gq.aggregate(
        consumer, "Reading", "raw_score", _author_dispatch=_AUTHOR_DISPATCH
    ) == pytest.approx(6.0)
    assert (
        gq.aggregate(
            consumer, "Reading", "raw_score", func="count", _author_dispatch=_AUTHOR_DISPATCH
        )
        == 3
    )


# -- finding 2a: an empty contributor rule list is not a de-dup guarantee ---


def test_empty_contributor_rules_list_does_not_open_the_hidden_field_exemption(
    make_registry: RegistryFactory,
    make_policy: PolicyFactory,
    make_store: StoreFactory,
) -> None:
    """`contributor_rules={"Reading": []}` put Reading in the mapping without
    declaring any way to resolve a contributor.

    Membership alone opened the hidden-field exemption, and every row then
    failed to resolve -- so the exemption argued from a distinct-identity
    guarantee that nothing could provide. The gate must require a rule the
    engine can actually run.
    """
    registry = _contributor_registry(make_registry)
    store = make_store(registry)
    _seed_two_shelves(store)
    for i in range(3):
        store.insert("Reader", {"id": f"reader-{i}"}, SRC)
        store.insert(
            "Reading",
            {"id": f"r{i}", "shelf_id": "shelf-1", "raw_score": 7.0},
            SRC,
        )
        store.create_link("byReader", f"r{i}", f"reader-{i}")
    policy = _contributor_policy(make_policy)
    policy.contributor_rules = {**policy.contributor_rules, "Reading": []}
    gq = GuardedQuery(store, registry, policy)

    consumer = _human("shelf", "shelf-1")
    with raises_code(VisibilityError, "VISIBILITY_DENIED"):
        gq.aggregate(consumer, "Reading", "raw_score", _author_dispatch=_AUTHOR_DISPATCH)


def test_scope_policy_validate_rejects_an_empty_contributor_rule_list(
    make_registry: RegistryFactory,
    make_policy: PolicyFactory,
) -> None:
    """Declaration time refuses it too, so the author hears about it once at
    startup rather than through a silently weaker floor at query time.

    Scoped to `contributor_rules`: an empty `rules` entry resolves no level
    and `covers_scope` then DENIES, so it fails closed. An empty
    `contributor_rules` entry failed open -- opting into identity de-dup
    while providing no identity. Only the second is a policy error.
    """
    registry = _contributor_registry(make_registry)
    policy = _contributor_policy(make_policy)
    policy.contributor_rules["Reading"] = []

    with raises_code(ValidationFailed, "SCOPE_POLICY_ERROR"):
        policy.validate(registry)


def test_scope_policy_validate_allows_an_empty_scope_rule_list(
    make_registry: RegistryFactory,
    make_policy: PolicyFactory,
) -> None:
    """The deliberate asymmetry, pinned so a later tidy-up does not
    "consistently" reject both: an empty `rules` list denies rather than
    releases, and some types legitimately declare no scope rule."""
    registry = _contributor_registry(make_registry)
    policy = _contributor_policy(make_policy)
    policy.rules["Reading"] = []

    policy.validate(registry)


# -- finding 2b: link closure and retirement must not defeat the floor -----


def test_aggregate_min_n_survives_contributor_link_closure(
    make_registry: RegistryFactory,
    make_policy: PolicyFactory,
    make_store: StoreFactory,
) -> None:
    """3 Readings by ONE Reader are correctly refused; closing the byReader
    links must not turn that refusal into a release.

    Closing the links only destroys the engine's ability to SEE that the
    three rows share an author. It does not create two more people.
    """
    registry = _contributor_registry(make_registry)
    store = make_store(registry)
    _seed_two_shelves(store)
    store.insert("Reader", {"id": "reader-1"}, SRC)
    for i, value in enumerate((10.0, 20.0, 30.0)):
        store.insert("Reading", {"id": f"r{i}", "shelf_id": "shelf-1", "score": value}, SRC)
        store.create_link("byReader", f"r{i}", "reader-1")
    gq = GuardedQuery(store, registry, _contributor_policy(make_policy))
    consumer = _human("shelf", "shelf-1")
    with raises_code(VisibilityError, "MIN_N_VIOLATION"):
        gq.aggregate(consumer, "Reading", "score")

    for i in range(3):
        store.close_link("byReader", f"r{i}", "reader-1")

    with raises_code(VisibilityError, "MIN_N_VIOLATION"):
        gq.aggregate(consumer, "Reading", "score")
    with raises_code(VisibilityError, "MIN_N_VIOLATION"):
        gq.count_contributors(consumer, "Reading")


def test_aggregate_min_n_survives_partial_contributor_link_closure(
    make_registry: RegistryFactory,
    make_policy: PolicyFactory,
    make_store: StoreFactory,
) -> None:
    """Closing only SOME links was enough: 1 still-resolved Reader plus 2
    unresolved rows reached the floor. Pinned separately because a fix that
    only handles the all-unresolved case leaves this arithmetic open."""
    registry = _contributor_registry(make_registry)
    store = make_store(registry)
    _seed_two_shelves(store)
    store.insert("Reader", {"id": "reader-1"}, SRC)
    for i, value in enumerate((10.0, 20.0, 30.0)):
        store.insert("Reading", {"id": f"r{i}", "shelf_id": "shelf-1", "score": value}, SRC)
        store.create_link("byReader", f"r{i}", "reader-1")
    store.close_link("byReader", "r0", "reader-1")
    store.close_link("byReader", "r1", "reader-1")
    gq = GuardedQuery(store, registry, _contributor_policy(make_policy))

    consumer = _human("shelf", "shelf-1")
    with raises_code(VisibilityError, "MIN_N_VIOLATION"):
        gq.aggregate(consumer, "Reading", "score")


def test_aggregate_min_n_survives_contributor_retirement(
    make_registry: RegistryFactory,
    make_policy: PolicyFactory,
    make_store: StoreFactory,
) -> None:
    """Retiring the Reader ROW alone -- every byReader link still live --
    also broke resolution, because `resolve_contributor` returns `None` for
    an object that no longer reads current. A second, independent route to
    the same hole, so it gets its own name."""
    registry = _contributor_registry(make_registry)
    store = make_store(registry)
    _seed_two_shelves(store)
    store.insert("Reader", {"id": "reader-1"}, SRC)
    for i, value in enumerate((10.0, 20.0, 30.0)):
        store.insert("Reading", {"id": f"r{i}", "shelf_id": "shelf-1", "score": value}, SRC)
        store.create_link("byReader", f"r{i}", "reader-1")
    store.retire_object("Reader", "reader-1")
    assert store.links_from("byReader", "r0") == ["reader-1"]
    gq = GuardedQuery(store, registry, _contributor_policy(make_policy))

    consumer = _human("shelf", "shelf-1")
    with raises_code(VisibilityError, "MIN_N_VIOLATION"):
        gq.aggregate(consumer, "Reading", "score")


def test_retire_action_does_not_release_an_individuals_hidden_values(
    make_registry: RegistryFactory,
    make_policy: PolicyFactory,
    make_store: StoreFactory,
) -> None:
    """The end-to-end shape this actually takes in a deployed ontology.

    A declared Action calling `ctx.retire` -- the sanctioned retire plus
    two-directional link cascade -- turned a correct `MIN_N_VIOLATION` into
    the released mean of one person's `human_visible=False` values. No
    operator misuse and no direct store access: ordinary lifecycle.
    """
    registry = _contributor_registry(make_registry, owned=True)
    registry.register_action_type(
        ActionTypeDef(
            api_name="RetireReader",
            display_name="Retire Reader",
            target_type="Reader",
            executable_by_roles=["Operator"],
            description="Retire a Reader and cascade its links",
            parameters=[ActionParameterDef(name="reader_id", type="str", required=True)],
        )
    )
    store = make_store(registry)
    _seed_two_shelves(store)
    store.insert("Reader", {"id": "reader-1"}, SRC)
    for i, value in enumerate((10.0, 20.0, 30.0)):
        store.insert(
            "Reading",
            {"id": f"r{i}", "shelf_id": "shelf-1", "raw_score": value},
            SRC,
        )
        store.create_link("byReader", f"r{i}", "reader-1")

    policy = _contributor_policy(make_policy)
    gq = GuardedQuery(store, registry, policy)
    consumer = _human("shelf", "shelf-1")
    with raises_code(VisibilityError, "MIN_N_VIOLATION"):
        gq.aggregate(consumer, "Reading", "raw_score", _author_dispatch=_AUTHOR_DISPATCH)

    class RetireReaderParams(ActionParams):
        reader_id: str

    def retire_reader(ctx: ActionContext, params: RetireReaderParams) -> dict[str, str]:
        ctx.retire("Reader", params.reader_id)
        return {"reader_id": params.reader_id}

    executor = ActionExecutor(store, registry, policy)
    executor._register("RetireReader", retire_reader, RetireReaderParams)
    executor.execute(
        Consumer(
            actor_id="op-1",
            role="Operator",
            scope_level="shelf",
            scope_id="shelf-1",
            kind="human",
        ),
        "RetireReader",
        {"reader_id": "reader-1"},
    )
    assert store.links_from("byReader", "r0") == []

    with raises_code(VisibilityError, "MIN_N_VIOLATION"):
        gq.aggregate(consumer, "Reading", "raw_score", _author_dispatch=_AUTHOR_DISPATCH)


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
    assert [r.payload["id"] for r in gq_a.get_objects(consumer, "Book", limit=None)] == ["book-1"]
    assert [r.payload["id"] for r in gq_b.get_objects(consumer, "Book", limit=None)] == ["book-9"]

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

    assert {r.payload["id"] for r in gq.get_objects(human, "Book", limit=None)} == {"book-free"}
    assert {r.payload["id"] for r in gq.get_objects(ai, "Book", limit=None)} == {
        "book-free",
        "book-loaned",
    }


def test_count_matches_scoped_visible_rows_without_redacting(
    make_registry: RegistryFactory,
    make_policy: PolicyFactory,
    make_store: StoreFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Count keeps scope and row visibility, but never builds payload copies.

    The human sees only ``book-free``: ``book-loaned`` is hidden by the row
    predicate and ``book-other`` is outside the consumer's shelf scope. The
    surviving row carries a human-hidden field, so the following list read
    proves redaction still happens on the payload-returning surface.
    """
    registry = _library_registry(make_registry)
    store = make_store(registry)
    _seed_two_shelves(store)
    store.insert(
        "Book",
        {"id": "book-free", "shelf_id": "shelf-1", "acquisition_cost": 42.0},
        SRC,
    )
    store.insert(
        "Book",
        {"id": "book-loaned", "shelf_id": "shelf-1", "acquisition_cost": 99.0},
        SRC,
    )
    store.insert(
        "Book",
        {"id": "book-other", "shelf_id": "shelf-2", "acquisition_cost": 7.0},
        SRC,
    )
    store.insert("Loan", {"id": "loan-1"}, SRC)
    store.create_link("loanedBook", "loan-1", "book-loaned")
    gq = GuardedQuery(store, registry, _row_visibility_policy(make_policy))
    consumer = _human("shelf", "shelf-1")

    original_redact = gq._redact
    redact_calls: list[tuple[Any, ...]] = []

    def record_redact(*args: Any, **kwargs: Any) -> StoredObject:
        redact_calls.append(args)
        return original_redact(*args, **kwargs)

    monkeypatch.setattr(gq, "_redact", record_redact)

    count = gq.count(consumer, "Book")
    assert count == 1
    assert redact_calls == []

    rows = gq.get_objects(consumer, "Book", limit=None)
    assert count == len(rows)
    assert [row.payload["id"] for row in rows] == ["book-free"]
    assert "acquisition_cost" not in rows[0].payload
    assert len(redact_calls) == len(rows) == 1


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
    gq = GuardedQuery(store, registry, _library_policy(make_policy))  # no row_visibility here

    human = _human("shelf", "shelf-1")
    assert gq.get_object(human, "Book", "book-loaned") is not None
    assert {r.payload["id"] for r in gq.get_objects(human, "Book", limit=None)} == {
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
    `DirectProperty` key -- exempt from the supplied-value where= gate --
    AND a sensitivity-hidden field. `aggregate`'s value_field gate must use
    `_hidden_fields(..., disclosure="learned")`, not `disclosure="supplied"`:
    a consumer may still FILTER by its own scope key, but aggregating BY
    that same hidden field is still denied -- the two gates are deliberately
    asymmetric (see `GuardedQuery.aggregate`'s docstring comment)."""
    registry = _shared_field_registry(make_registry)
    store = make_store(registry)
    _hide_record_b_group_id(registry)
    store.insert("RecordB", {"id": "b-1", "group_id": "secret-group"}, SRC)
    gq = GuardedQuery(store, registry, _shared_field_policy(make_policy))

    consumer = _human("group", "secret-group")

    # where= is still exempt (scope-routing DirectProperty key).
    results = gq.get_objects(consumer, "RecordB", where={"group_id": "secret-group"}, limit=None)
    assert [r.payload["id"] for r in results] == ["b-1"]

    # value_field is NOT exempt: still denied.
    with raises_code(VisibilityError, "VISIBILITY_DENIED"):
        gq.aggregate(consumer, "RecordB", "group_id")


@pytest.mark.parametrize(
    "condition",
    ["secret-group", {"in": ["secret-group"]}],
    ids=["eq", "in-list"],
)
def test_supply_shaped_where_honors_scope_key_exemption_on_both_gates(
    condition: object,
    make_registry: RegistryFactory,
    make_policy: PolicyFactory,
    make_store: StoreFactory,
) -> None:
    registry = _shared_field_registry(make_registry)
    _hide_record_b_group_id(registry)
    store = make_store(registry)
    for i in range(3):
        store.insert(
            "RecordB",
            {
                "id": f"b-{i + 1}",
                "group_id": "secret-group",
                "score": float(i + 1),
            },
            SRC,
        )
    gq = GuardedQuery(store, registry, _shared_field_policy(make_policy))

    consumer = _human("group", "secret-group")
    where = {"group_id": condition}

    rows = gq.get_objects(consumer, "RecordB", where=where, limit=None)
    result = gq.aggregate(
        consumer,
        "RecordB",
        "score",
        where=where,
    )
    contributor_count = gq.count_contributors(consumer, "RecordB", where=where)

    assert [row.payload["id"] for row in rows] == ["b-1", "b-2", "b-3"]
    assert result == 2.0
    assert contributor_count == 3


def test_aggregate_hidden_field_exempt_when_contributor_dedup_declared(
    make_registry: RegistryFactory,
    make_policy: PolicyFactory,
    make_store: StoreFactory,
) -> None:
    """Consumer reads are refused; author provenance preserves the AC10 seam."""
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
    with raises_code(VisibilityError, "VISIBILITY_DENIED"):
        gq.aggregate(human, "Reading", "raw_score")
    result = gq.aggregate(human, "Reading", "raw_score", _author_dispatch=_AUTHOR_DISPATCH)
    assert result == pytest.approx(4.0)


def test_consumer_mean_ne_differencing_of_hidden_field_is_refused(
    make_registry: RegistryFactory,
    make_policy: PolicyFactory,
    make_store: StoreFactory,
) -> None:
    """A consumer must never obtain the two means needed to solve for one row.

    The four hidden values are deliberately distinct and every row has a
    distinct contributor, so both selections satisfy ``min_n=3``. The gate
    must refuse both the all-row mean and the ``ne``-sliced mean before either
    side of the differencing oracle is released.
    """
    registry = _contributor_registry(make_registry)
    store = make_store(registry)
    _seed_two_shelves(store)
    hidden_values = (1.0, 4.0, 7.0, 13.0)
    for i, value in enumerate(hidden_values):
        store.insert("Reader", {"id": f"reader-{i}"}, SRC)
        store.insert(
            "Reading",
            {"id": f"r{i}", "shelf_id": "shelf-1", "raw_score": value},
            SRC,
        )
        store.create_link("byReader", f"r{i}", f"reader-{i}")
    query = GuardedQuery(store, registry, _contributor_policy(make_policy))
    human = _human("shelf", "shelf-1")

    with raises_code(VisibilityError, "VISIBILITY_DENIED"):
        query.aggregate(human, "Reading", "raw_score", func="mean")
    with raises_code(VisibilityError, "VISIBILITY_DENIED"):
        query.aggregate(
            human,
            "Reading",
            "raw_score",
            where={"id": {"ne": "r0"}},
            func="mean",
        )


def _distinct_contributor_aggregate_query(
    make_registry: RegistryFactory,
    make_policy: PolicyFactory,
    make_store: StoreFactory,
) -> tuple[GuardedQuery, Consumer, tuple[float, ...]]:
    registry = _contributor_registry(make_registry)
    store = make_store(registry)
    _seed_two_shelves(store)
    seeded_values = (1.0, 4.0, 7.0)
    for i, value in enumerate(seeded_values):
        store.insert("Reader", {"id": f"reader-{i}"}, SRC)
        store.insert(
            "Reading",
            {
                "id": f"r{i}",
                "shelf_id": "shelf-1",
                "score": value,
                "raw_score": value,
            },
            SRC,
        )
        store.create_link("byReader", f"r{i}", f"reader-{i}")
    hidden_values = tuple(
        float(row.payload["raw_score"])
        for row in store.read_all("Reading")
        if row.lineage.object_id in {"r0", "r1", "r2"}
    )
    assert len(hidden_values) == len(seeded_values)
    assert len(set(hidden_values)) == len(hidden_values)
    query = GuardedQuery(store, registry, _contributor_policy(make_policy))
    return query, _human("shelf", "shelf-1"), hidden_values


@pytest.mark.parametrize("func", ["min", "max", "sum"])
def test_contributor_exemption_refuses_disclosing_hidden_aggregate_functions(
    func: str,
    make_registry: RegistryFactory,
    make_policy: PolicyFactory,
    make_store: StoreFactory,
) -> None:
    """The contributor identity guarantee cannot release individual hidden
    values through min/max or reconstruct one through sum plus count."""
    query, human, hidden_values = _distinct_contributor_aggregate_query(
        make_registry, make_policy, make_store
    )

    for group_by in (None, "shelf_id"):
        with raises_code(VisibilityError, "VISIBILITY_DENIED") as excinfo:
            if group_by is None:
                query.aggregate(human, "Reading", "raw_score", func=func)
            else:
                query.aggregate_by(
                    human,
                    "Reading",
                    "raw_score",
                    group_by,
                    func=func,
                )
        message = str(excinfo.value)
        for hidden_value in hidden_values:
            assert repr(hidden_value) not in message


@pytest.mark.parametrize("func", ["min", "max", "sum"], ids=["min", "max", "sum"])
def test_contributor_exemption_refuses_disclosing_hidden_aggregate_functions_for_author_origin(
    func: str,
    make_registry: RegistryFactory,
    make_policy: PolicyFactory,
    make_store: StoreFactory,
) -> None:
    """Author provenance preserves the separate min/max/sum restriction."""
    query, human, hidden_values = _distinct_contributor_aggregate_query(
        make_registry, make_policy, make_store
    )

    for group_by in (None, "shelf_id"):
        with raises_code(VisibilityError, "VISIBILITY_DENIED") as excinfo:
            if group_by is None:
                query.aggregate(
                    human,
                    "Reading",
                    "raw_score",
                    func=func,
                    _author_dispatch=_AUTHOR_DISPATCH,
                )
            else:
                query.aggregate_by(
                    human,
                    "Reading",
                    "raw_score",
                    group_by,
                    func=func,
                    _author_dispatch=_AUTHOR_DISPATCH,
                )
        message = str(excinfo.value)
        for hidden_value in hidden_values:
            assert repr(hidden_value) not in message


@pytest.mark.parametrize(("func", "expected"), [("mean", 4.0), ("count", 3)])
def test_contributor_exemption_releases_hidden_mean_and_count(
    func: str,
    expected: int | float,
    make_registry: RegistryFactory,
    make_policy: PolicyFactory,
    make_store: StoreFactory,
) -> None:
    query, human, hidden_values = _distinct_contributor_aggregate_query(
        make_registry, make_policy, make_store
    )

    # The old consumer-origin outcome is intentionally changed: the same
    # contributor declaration is not an exemption for a direct consumer read.
    with raises_code(VisibilityError, "VISIBILITY_DENIED"):
        query.aggregate(human, "Reading", "raw_score", func=func)
    with raises_code(VisibilityError, "VISIBILITY_DENIED"):
        query.aggregate_by(human, "Reading", "raw_score", "shelf_id", func=func)

    result = query.aggregate(
        human,
        "Reading",
        "raw_score",
        func=func,
        _author_dispatch=_AUTHOR_DISPATCH,
    )
    with raises_code(VisibilityError, "VISIBILITY_DENIED"):
        query.aggregate_by(
            human,
            "Reading",
            "raw_score",
            "shelf_id",
            func=func,
            _author_dispatch=_AUTHOR_DISPATCH,
        )
    assert result == pytest.approx(expected)
    if func == "mean":
        assert result != min(hidden_values)
        assert result != max(hidden_values)
        assert result != sum(hidden_values)


# -- finding 4: author provenance is minted, never asserted -----------------
#
# What these three pin is the input the suite could not previously
# CONSTRUCT: a caller that claims author provenance without a declared
# Function behind it. Before this fix there was no such caller, because
# claiming the tier WAS the grant -- `origin="author_function"` was a public
# keyword on the signature above, and `BoundQuery` carried the tier as a
# class attribute, so both spellings below returned the hidden mean (4.0)
# rather than refusing. Every fixture that reached the author tier reached it
# by asserting it, so no assertion in the suite could tell a mint from a
# claim.


def test_asserting_author_provenance_as_a_value_does_not_grant_it(
    make_registry: RegistryFactory,
    make_policy: PolicyFactory,
    make_store: StoreFactory,
) -> None:
    """The grant is compared by IDENTITY against the engine's one instance,
    so no value a caller can write reproduces it -- not the old
    `"author_function"` string, not a truthy stand-in, and not a fresh
    instance of the grant's own class.

    The same selection releases 4.0 under the real mint (the test above),
    which is what makes each refusal here attributable to provenance and
    not to some unrelated gate.
    """
    query, human, _hidden = _distinct_contributor_aggregate_query(
        make_registry, make_policy, make_store
    )

    for claimed in ("author_function", True, object(), _AuthorDispatch()):
        with raises_code(VisibilityError, "VISIBILITY_DENIED"):
            query.aggregate(human, "Reading", "raw_score", _author_dispatch=claimed)


def test_public_boundquery_construction_reads_at_consumer_tier(
    make_registry: RegistryFactory,
    make_policy: PolicyFactory,
    make_store: StoreFactory,
) -> None:
    """`BoundQuery` is exported, so building one was the second, quieter way
    to assert author provenance: it needed no keyword at all, and reached the
    author tier without any declared Function in the picture.

    A hand-built `BoundQuery` now carries no grant, exactly as it carries no
    declared capabilities -- both are authorities of a DISPATCH.
    """
    query, human, _hidden = _distinct_contributor_aggregate_query(
        make_registry, make_policy, make_store
    )

    bound = BoundQuery(query, human)
    with raises_code(VisibilityError, "VISIBILITY_DENIED"):
        bound.aggregate("Reading", "raw_score")


def test_public_boundquery_construction_reads_at_consumer_tier_when_grouped(
    make_registry: RegistryFactory,
    make_policy: PolicyFactory,
    make_store: StoreFactory,
) -> None:
    """The grouped face of the same door, on its own name.

    `aggregate` and `aggregate_by` pass the grant through separate call
    sites in `_TypedReadMixin`; a fix that threaded only the ungrouped one
    leaves this open, and a shared assertion could not say which.
    """
    query, human, _hidden = _distinct_contributor_aggregate_query(
        make_registry, make_policy, make_store
    )

    bound = BoundQuery(query, human)
    with raises_code(VisibilityError, "VISIBILITY_DENIED"):
        bound.aggregate_by("Reading", "raw_score", "shelf_id")


@pytest.mark.parametrize(
    ("func", "expected"),
    [
        ("mean", 4.0),
        ("count", 3),
        ("sum", 12.0),
        ("min", 1.0),
        ("max", 7.0),
    ],
)
def test_contributor_type_visible_field_keeps_every_aggregate_function(
    func: str,
    expected: int | float,
    make_registry: RegistryFactory,
    make_policy: PolicyFactory,
    make_store: StoreFactory,
) -> None:
    query, human, _ = _distinct_contributor_aggregate_query(make_registry, make_policy, make_store)

    assert query.aggregate(human, "Reading", "score", func=func) == pytest.approx(expected)


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
        store.insert("Reading", {"id": f"r{i}", "shelf_id": "shelf-1", "score": 4.0}, SRC)
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
        store.insert("Reading", {"id": f"r{i}", "shelf_id": "shelf-1", "score": 4.0}, SRC)
        store.create_link("byReader", f"r{i}", f"reader-{i}")
    # shelf-2: a bigger population the shelf-1 consumer must not see.
    for i in range(5):
        store.insert("Reader", {"id": f"other-{i}"}, SRC)
        store.insert("Reading", {"id": f"o{i}", "shelf_id": "shelf-2", "score": 1.0}, SRC)
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
        store.insert("Reading", {"id": f"r{i}", "shelf_id": "shelf-1", "score": 4.0}, SRC)
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
        store.insert("Reading", {"id": f"r{i}", "shelf_id": "shelf-1", "score": 4.0}, SRC)
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
        store.insert("Book", {"id": f"book-{i}", "shelf_id": "shelf-1", "rating": 4.0}, SRC)
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
        store.insert("Book", {"id": f"book-{i}", "shelf_id": "shelf-1", "rating": 4.0}, SRC)
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
        gq.count_contributors(_human("shelf", "shelf-1"), "Reading", where={"raw_score": 1.0})


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
