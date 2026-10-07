"""Every read refuses an undeclared object type, first and always (#194).

A read of a type the ontology does not declare used to answer as an empty,
scope-limited result -- indistinguishable from "you cannot see these rows".
The type check is now the first statement of the disclosure gate, so every
public `GuardedQuery` read, every string-form `OntologyClient` read, and every
`BoundQuery` string read refuses with `UNKNOWN_OBJECT_TYPE` before any policy,
`where`, `group_by`, or store check.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest
from conftest import raises_code

from ontary.client import OntologyClient
from ontary.errors import ValidationFailed
from ontary.functions import BoundQuery, FunctionRegistry
from ontary.meta import (
    Cardinality,
    FunctionDef,
    LinkTypeDef,
    ObjectTypeDef,
    OntologyRegistry,
)
from ontary.ontology import OntologyDef
from ontary.query import GuardedQuery
from ontary.scope import DirectProperty, ScopePolicy, SelfScope
from ontary.security import Consumer
from ontary.store import Source, Store

SRC = Source(source_system="test")
NOPE = "Nope"
NOPE_MESSAGE = f"unregistered object type: {NOPE!r}"

ObjectTypeFactory = Callable[..., ObjectTypeDef]
RegistryFactory = Callable[..., OntologyRegistry]
PolicyFactory = Callable[..., ScopePolicy]
StoreFactory = Callable[..., Store]

Invoker = Callable[[GuardedQuery, Consumer], object]

# One entry per public `GuardedQuery` read, each naming the undeclared type.
# `traverse` takes no anchor type, so its undeclared-anchor case is pinned
# through `OntologyClient.traverse` below; its entry here only keeps the
# coverage check honest (it reads a declared link and must not refuse).
UNKNOWN_TYPE_INVOKERS: dict[str, Invoker] = {
    "get_objects": lambda q, c: q.get_objects(c, NOPE, limit=None),
    "get_object": lambda q, c: q.get_object(c, NOPE, "x"),
    "scope_limited": lambda q, _c: q.scope_limited(NOPE),
    "redacted_fields": lambda q, c: q.redacted_fields(c, NOPE),
    "count": lambda q, c: q.count(c, NOPE),
    "exists": lambda q, c: q.exists(c, NOPE),
    "aggregate": lambda q, c: q.aggregate(c, NOPE, "score"),
    "aggregate_by": lambda q, c: q.aggregate_by(c, NOPE, "score", "shelf_id"),
    "count_contributors": lambda q, c: q.count_contributors(c, NOPE),
    "visible_events": lambda q, c: q.visible_events(c, about=(NOPE, "x")),
}
COVERED_ELSEWHERE = {"traverse"}


def _registry(
    make_registry: RegistryFactory, make_object_type: ObjectTypeFactory
) -> OntologyRegistry:
    return make_registry(
        object_types=[
            make_object_type("Shelf"),
            make_object_type("Book", ("shelf_id", "str"), ("score", "float")),
        ],
        link_types=[
            LinkTypeDef(
                api_name="onShelf",
                from_type="Book",
                to_type="Shelf",
                cardinality=Cardinality.MANY_TO_ONE,
                description="Book -> the Shelf holding it",
            )
        ],
        functions=[
            FunctionDef(
                api_name="listNope",
                description="Read an undeclared type from a Function body",
                input_description="none",
                output_description="list",
            )
        ],
    )


def _policy(make_policy: PolicyFactory) -> ScopePolicy:
    return make_policy(
        levels=["shelf"],
        rules={
            "Shelf": [SelfScope(level="shelf")],
            "Book": [DirectProperty(level="shelf", property_name="shelf_id")],
        },
        min_n=1,
    )


def _seeded_store(make_store: StoreFactory, registry: OntologyRegistry) -> Store:
    store = make_store(registry)
    for shelf in ("shelf-1", "shelf-2"):
        store.insert("Shelf", {"id": shelf}, SRC)
        for n in range(3):
            book_id = f"{shelf}-book-{n}"
            store.insert(
                "Book", {"id": book_id, "shelf_id": shelf, "score": float(n)}, SRC
            )
            store.create_link("onShelf", book_id, shelf)
    return store


def _human() -> Consumer:
    return Consumer(
        actor_id="u1", role="Member", scope_level="shelf", scope_id="shelf-1", kind="human"
    )


def _ai() -> Consumer:
    return Consumer(
        actor_id="agent-1", role="Agent", scope_level="shelf", scope_id="shelf-2", kind="ai"
    )


@pytest.fixture
def query(
    make_registry: RegistryFactory,
    make_object_type: ObjectTypeFactory,
    make_policy: PolicyFactory,
    make_store: StoreFactory,
) -> GuardedQuery:
    registry = _registry(make_registry, make_object_type)
    return GuardedQuery(_seeded_store(make_store, registry), registry, _policy(make_policy))


@pytest.fixture
def client(
    make_registry: RegistryFactory,
    make_object_type: ObjectTypeFactory,
    make_policy: PolicyFactory,
    make_store: StoreFactory,
) -> OntologyClient:
    registry = _registry(make_registry, make_object_type)
    functions = FunctionRegistry(registry)

    def _list_nope(bound: BoundQuery) -> object:
        return bound.list(NOPE, limit=None)

    functions.register("listNope", _list_nope)
    ontology = OntologyDef(
        name="shelves",
        registry=registry,
        policy=_policy(make_policy),
        functions=functions,
    )
    ontology.validate()
    return OntologyClient(ontology, _seeded_store(make_store, registry), _human())


def test_every_public_guarded_read_is_covered_here() -> None:
    """A new public read must join this file's refusal matrix."""
    public_reads = {
        name
        for name, member in GuardedQuery.__dict__.items()
        if not name.startswith("_") and callable(member)
    }
    assert public_reads == set(UNKNOWN_TYPE_INVOKERS) | COVERED_ELSEWHERE


@pytest.mark.parametrize("read", sorted(UNKNOWN_TYPE_INVOKERS))
def test_every_public_guarded_read_refuses_an_undeclared_type(
    query: GuardedQuery, read: str
) -> None:
    with raises_code(ValidationFailed, "UNKNOWN_OBJECT_TYPE") as excinfo:
        UNKNOWN_TYPE_INVOKERS[read](query, _human())
    assert str(excinfo.value) == NOPE_MESSAGE
    assert excinfo.value.kind == "validation"


@pytest.mark.parametrize(
    ("label", "invoke"),
    [
        (
            "unknown where operator",
            lambda q, c: q.get_objects(c, NOPE, where={"id": {"bogus": 1}}, limit=None),
        ),
        (
            "unknown where key",
            lambda q, c: q.get_objects(c, NOPE, where={"missing": "x"}, limit=None),
        ),
        (
            "unknown where operator on count",
            lambda q, c: q.count(c, NOPE, where={"id": {"bogus": 1}}),
        ),
        (
            "order_by on an unknown field",
            lambda q, c: q.get_objects(c, NOPE, order_by="missing", limit=10),
        ),
        (
            "malformed after cursor",
            lambda q, c: q.get_objects(c, NOPE, limit=10, after="not-a-cursor"),
        ),
        (
            "empty group_by",
            lambda q, c: q.aggregate_by(c, NOPE, "score", ""),
        ),
        (
            "unknown group_by",
            lambda q, c: q.aggregate_by(c, NOPE, "score", "missing"),
        ),
        (
            "unknown value_field",
            lambda q, c: q.aggregate(c, NOPE, "missing"),
        ),
        (
            "unknown value_field with a malformed where",
            lambda q, c: q.aggregate(c, NOPE, "missing", {"id": {"bogus": 1}}),
        ),
    ],
)
def test_the_type_check_runs_before_every_parameter_check(
    query: GuardedQuery, label: str, invoke: Invoker
) -> None:
    with raises_code(ValidationFailed, "UNKNOWN_OBJECT_TYPE"):
        invoke(query, _human())


@pytest.mark.parametrize("read", sorted(UNKNOWN_TYPE_INVOKERS))
def test_the_type_check_runs_before_the_scope_coherence_gate(
    query: GuardedQuery, read: str
) -> None:
    # The SCOPE_POLICY_ERROR trigger: one name both unscoped and scope-routed.
    # `unscoped_types` and `rules` are mutable, so build it past validation.
    query._policy.unscoped_types.add(NOPE)
    query._policy.rules[NOPE] = [DirectProperty(level="shelf", property_name="id")]

    with raises_code(ValidationFailed, "UNKNOWN_OBJECT_TYPE"):
        UNKNOWN_TYPE_INVOKERS[read](query, _human())


@pytest.mark.parametrize("read", sorted(UNKNOWN_TYPE_INVOKERS))
def test_the_refusal_is_the_same_for_every_caller(
    query: GuardedQuery, read: str
) -> None:
    refusals: list[tuple[str | None, str]] = []
    for consumer in (_human(), _ai()):
        with pytest.raises(ValidationFailed) as excinfo:
            UNKNOWN_TYPE_INVOKERS[read](query, consumer)
        refusals.append((excinfo.value.code, str(excinfo.value)))
    assert refusals == [("UNKNOWN_OBJECT_TYPE", NOPE_MESSAGE)] * 2


@pytest.mark.parametrize("read", sorted(UNKNOWN_TYPE_INVOKERS))
def test_the_refusal_never_reads_a_row(
    query: GuardedQuery, monkeypatch: pytest.MonkeyPatch, read: str
) -> None:
    def _no_row_may_be_checked(*_args: object, **_kwargs: object) -> bool:
        raise AssertionError("an undeclared type reached _visible")

    monkeypatch.setattr(GuardedQuery, "_visible", _no_row_may_be_checked)
    with raises_code(ValidationFailed, "UNKNOWN_OBJECT_TYPE"):
        UNKNOWN_TYPE_INVOKERS[read](query, _human())


@pytest.mark.parametrize("disclosure", ["supplied", "learned"])
def test_hidden_fields_refuses_an_undeclared_type(
    query: GuardedQuery, disclosure: Any
) -> None:
    """Never "nothing hidden" for a type with no declaration to consult."""
    with raises_code(ValidationFailed, "UNKNOWN_OBJECT_TYPE"):
        query._hidden_fields(_human(), NOPE, disclosure=disclosure)


def test_declared_types_still_read_as_before(query: GuardedQuery) -> None:
    human = _human()
    assert query.count(human, "Book") == 3
    assert query.scope_limited("Book") is True
    assert query.redacted_fields(human, "Book") == ()


@pytest.mark.parametrize(
    ("label", "invoke"),
    [
        ("list", lambda c: c.list(NOPE, limit=None)),
        ("list paged", lambda c: c.list(NOPE)),
        ("list where", lambda c: c.list(NOPE, {"id": "x"}, limit=None)),
        ("count", lambda c: c.count(NOPE)),
        ("get", lambda c: c.get(NOPE, "x")),
        ("exists", lambda c: c.exists(NOPE)),
        ("aggregate", lambda c: c.aggregate(NOPE, "score")),
        ("aggregate count", lambda c: c.aggregate(NOPE, func="count")),
        ("aggregate_by", lambda c: c.aggregate_by(NOPE, "score", "shelf_id")),
        ("aggregate_by empty group", lambda c: c.aggregate_by(NOPE, "score", "")),
    ],
)
def test_client_string_reads_refuse_an_undeclared_type(
    client: OntologyClient, label: str, invoke: Callable[[OntologyClient], object]
) -> None:
    with raises_code(ValidationFailed, "UNKNOWN_OBJECT_TYPE") as excinfo:
        invoke(client)
    assert str(excinfo.value) == NOPE_MESSAGE


def test_a_function_body_string_read_surfaces_the_refusal_unwrapped(
    client: OntologyClient,
) -> None:
    # `call_function` re-raises a handler's error as is: no FUNCTION_ERROR wrap.
    with raises_code(ValidationFailed, "UNKNOWN_OBJECT_TYPE") as excinfo:
        client.call_function("listNope", {})
    assert str(excinfo.value) == NOPE_MESSAGE


@pytest.mark.parametrize("reverse", [False, True])
def test_client_traverse_refuses_an_undeclared_anchor_first(
    client: OntologyClient, reverse: bool
) -> None:
    for link in ("onShelf", "noSuchLink"):
        with raises_code(ValidationFailed, "UNKNOWN_OBJECT_TYPE") as excinfo:
            client.traverse(NOPE, link, "x", reverse=reverse)
        assert str(excinfo.value) == NOPE_MESSAGE


def test_client_traverse_keeps_unknown_name_for_link_errors(
    client: OntologyClient,
) -> None:
    with raises_code(ValidationFailed, "UNKNOWN_NAME"):
        client.traverse("Book", "noSuchLink", "shelf-1-book-0")
    # Declared anchor on the wrong end of a declared link.
    with raises_code(ValidationFailed, "UNKNOWN_NAME"):
        client.traverse("Shelf", "onShelf", "shelf-1")
    with raises_code(ValidationFailed, "UNKNOWN_NAME"):
        client.traverse("Book", "onShelf", "shelf-1", reverse=True)
    assert [row.payload["id"] for row in client.traverse("Book", "onShelf", "shelf-1-book-0")] == [
        "shelf-1"
    ]


def test_require_object_type_resolves_or_refuses(client: OntologyClient) -> None:
    client._require_object_type("Book")
    with raises_code(ValidationFailed, "UNKNOWN_OBJECT_TYPE") as excinfo:
        client._require_object_type(NOPE)
    assert str(excinfo.value) == NOPE_MESSAGE
