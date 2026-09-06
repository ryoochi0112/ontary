"""Dedicated unit tests for `ontary.functions` (spec AC16).

Covers `FunctionRegistry.register`/`function`/`call` (happy path, unknown
function name, undeclared api_name, double-registration, handler raising)
and `BoundQuery`'s read-only surface (what it exposes, and that it is bound
to exactly one `Consumer` -- the one its owning `OntologyClient`/caller
constructed it with, never a per-call argument the handler body could
vary).

Uses a small standalone "library" registry/store (same toy domain as
`tests/test_scope.py`/`tests/test_client.py`) rather than a full
`OntologyClient`, since `FunctionRegistry`/`BoundQuery` only need a
`GuardedQuery` + `Consumer`, not the whole client facade.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, cast

import pytest
from conftest import raises_code

from ontary.errors import PreconditionFailed, ValidationFailed, VisibilityError
from ontary.functions import BoundQuery, FunctionRegistry
from ontary.meta import (
    Cardinality,
    FunctionDef,
    LinkTypeDef,
    ObjectTypeDef,
    OntologyRegistry,
    PropertyDef,
)
from ontary.model import LinkHandle
from ontary.query import GuardedQuery
from ontary.scope import DirectProperty, ScopePolicy, SelfScope
from ontary.security import Consumer
from ontary.store import ObjectStore, Source

SRC = Source(source_system="test")


RegistryFactory = Callable[..., OntologyRegistry]
PolicyFactory = Callable[..., ScopePolicy]
ConsumerFactory = Callable[..., Consumer]


def _functions_registry(make_registry: RegistryFactory) -> OntologyRegistry:
    return make_registry(
        object_types=[
            ObjectTypeDef(
                api_name="Shelf",
                display_name="Shelf",
                description="A shelf",
                layer="L0",
                properties=[PropertyDef(name="id", type="str")],
                primary_key="id",
            )
        ],
        functions=[
            FunctionDef(
                api_name="countShelves",
                description="counts shelves",
                input_description="none",
                output_description="int",
            )
        ],
    )


def _functions_policy(make_policy: PolicyFactory) -> ScopePolicy:
    return make_policy(
        levels=["shelf"],
        rules={"Shelf": [SelfScope(level="shelf")]},
        min_n=3,
    )


def _bound_query(
    registry: OntologyRegistry,
    make_consumer: ConsumerFactory,
    make_policy: PolicyFactory,
    scope_id: str = "shelf-1",
) -> BoundQuery:
    store = ObjectStore(registry)
    store.insert("Shelf", {"id": "shelf-1"}, SRC)
    store.insert("Shelf", {"id": "shelf-2"}, SRC)
    query = GuardedQuery(store, registry, _functions_policy(make_policy))
    return BoundQuery(
        query,
        make_consumer(
            actor_id="u1",
            role="Member",
            scope_level="shelf",
            scope_id=scope_id,
            kind="human",
        ),
    )


# -- FunctionRegistry.register / function ------------------------------------


def test_register_binds_handler_for_declared_function(
    make_registry: RegistryFactory,
    make_consumer: ConsumerFactory,
    make_policy: PolicyFactory,
) -> None:
    registry = _functions_registry(make_registry)
    functions = FunctionRegistry(registry)

    def _handler(query: BoundQuery, params: dict[str, Any]) -> int:
        return len(query.list("Shelf"))

    functions.register("countShelves", _handler)

    bound = _bound_query(registry, make_consumer, make_policy)
    assert functions.call("countShelves", bound, {}) == 1


def test_register_rejects_undeclared_function_name(
    make_registry: RegistryFactory,
) -> None:
    registry = _functions_registry(make_registry)
    functions = FunctionRegistry(registry)

    def _handler(query: BoundQuery, params: dict[str, Any]) -> int:
        return 0

    with raises_code(PreconditionFailed, "FUNCTION_ERROR") as exc_info:
        functions.register("noSuchFunction", _handler)
    assert "undeclared" in str(exc_info.value)


def test_register_rejects_double_registration(
    make_registry: RegistryFactory,
) -> None:
    registry = _functions_registry(make_registry)
    functions = FunctionRegistry(registry)

    def _handler(query: BoundQuery, params: dict[str, Any]) -> int:
        return 0

    functions.register("countShelves", _handler)
    with raises_code(PreconditionFailed, "FUNCTION_ERROR") as exc_info:
        functions.register("countShelves", _handler)
    assert "already registered" in str(exc_info.value)


def test_function_decorator_form_registers_and_returns_original_fn(
    make_registry: RegistryFactory,
    make_consumer: ConsumerFactory,
    make_policy: PolicyFactory,
) -> None:
    registry = _functions_registry(make_registry)
    functions = FunctionRegistry(registry)

    @functions.function("countShelves")
    def _handler(query: BoundQuery, params: dict[str, Any]) -> int:
        return 42

    # the decorator returns the original callable unchanged
    assert _handler(cast(BoundQuery, None), {}) == 42

    bound = _bound_query(registry, make_consumer, make_policy)
    assert functions.call("countShelves", bound, {}) == 42


# -- FunctionRegistry.call ----------------------------------------------------


def test_call_unknown_function_name_raises(
    make_registry: RegistryFactory,
    make_consumer: ConsumerFactory,
    make_policy: PolicyFactory,
) -> None:
    registry = _functions_registry(make_registry)
    functions = FunctionRegistry(registry)
    bound = _bound_query(registry, make_consumer, make_policy)

    with raises_code(PreconditionFailed, "FUNCTION_ERROR") as exc_info:
        functions.call("countShelves", bound, {})
    assert "no handler registered" in str(exc_info.value)


def test_call_propagates_handler_exception_unchanged(
    make_registry: RegistryFactory,
    make_consumer: ConsumerFactory,
    make_policy: PolicyFactory,
) -> None:
    registry = _functions_registry(make_registry)
    functions = FunctionRegistry(registry)

    def _boom(query: BoundQuery, params: dict[str, Any]) -> int:
        raise ValueError("handler blew up")

    functions.register("countShelves", _boom)
    bound = _bound_query(registry, make_consumer, make_policy)

    # `FunctionRegistry.call` does NOT wrap/translate a handler's own
    # exceptions into a function-registry error -- whatever the handler raises
    # propagates unchanged. Pinned as current behavior.
    with pytest.raises(ValueError, match="handler blew up"):
        functions.call("countShelves", bound, {})


def test_call_passes_params_through_to_handler(
    make_registry: RegistryFactory,
    make_consumer: ConsumerFactory,
    make_policy: PolicyFactory,
) -> None:
    registry = _functions_registry(make_registry)
    functions = FunctionRegistry(registry)
    seen: dict[str, Any] = {}

    def _handler(query: BoundQuery, params: dict[str, Any]) -> None:
        seen.update(params)

    functions.register("countShelves", _handler)
    bound = _bound_query(registry, make_consumer, make_policy)
    functions.call("countShelves", bound, {"limit": 5})
    assert seen == {"limit": 5}


# -- BoundQuery read-only surface --------------------------------------------


def test_bound_query_exposes_only_the_guarded_read_methods() -> None:
    public_names = {name for name in dir(BoundQuery) if not name.startswith("_")}
    # `capability` is a READ of the outside world (M5, AC6). A Function still
    # cannot write: no store handle, and no effect dispatchers either (AC4/AC5b).
    assert public_names == {
        "capability",
        "get",
        "list",
        "traverse",
        "aggregate",
        "aggregate_by",
        # `count_contributors` is a derived READ, min-N gated like the
        # aggregate it accompanies -- see `GuardedQuery.count_contributors`.
        "count_contributors",
    }


def test_bound_query_has_no_store_or_consumer_setter(
    make_registry: RegistryFactory,
    make_consumer: ConsumerFactory,
    make_policy: PolicyFactory,
) -> None:
    # No public way to swap the consumer or reach the underlying store/query
    # object after construction -- the binding is fixed for the lifetime of
    # the `BoundQuery` (module docstring: "closing the one bypass ... would
    # otherwise open").
    registry = _functions_registry(make_registry)
    bound = _bound_query(registry, make_consumer, make_policy)
    assert not hasattr(bound, "store")
    assert not hasattr(bound, "set_consumer")


def test_bound_query_reads_are_scoped_to_its_fixed_consumer(
    make_registry: RegistryFactory,
    make_consumer: ConsumerFactory,
    make_policy: PolicyFactory,
) -> None:
    registry = _functions_registry(make_registry)
    # shelf-1-scoped consumer only ever sees shelf-1, regardless of what a
    # handler tries to pass -- there is no per-call consumer argument at all.
    bound = _bound_query(
        registry, make_consumer, make_policy, scope_id="shelf-1"
    )
    objs = bound.list("Shelf")
    assert [o.payload["id"] for o in objs] == ["shelf-1"]


def test_bound_query_get_delegates_to_underlying_guarded_query(
    make_registry: RegistryFactory,
    make_consumer: ConsumerFactory,
    make_policy: PolicyFactory,
) -> None:
    registry = _functions_registry(make_registry)
    bound = _bound_query(
        registry, make_consumer, make_policy, scope_id="shelf-1"
    )
    row = bound.get("Shelf", "shelf-1")
    assert row is not None
    assert row.payload["id"] == "shelf-1"
    assert row.lineage.object_type == "Shelf"
    # out of scope -> `VISIBILITY_DENIED` propagates unchanged through the
    # BoundQuery, mirroring GuardedQuery.get_object exactly (BoundQuery adds
    # no guard logic of its own, per the module docstring).
    with raises_code(VisibilityError, "VISIBILITY_DENIED"):
        bound.get("Shelf", "shelf-2")


def test_bound_query_traverse_refuses_missing_from_id(
    make_registry: RegistryFactory,
    make_consumer: ConsumerFactory,
    make_policy: PolicyFactory,
) -> None:
    registry = make_registry(
        object_types=[
            ObjectTypeDef(
                api_name="Shelf",
                display_name="Shelf",
                description="A shelf",
                layer="L0",
                properties=[PropertyDef(name="id", type="str")],
                primary_key="id",
            )
        ],
        link_types=[
            LinkTypeDef(
                api_name="parentShelf",
                from_type="Shelf",
                to_type="Shelf",
                cardinality=Cardinality.MANY_TO_ONE,
                description="Shelf -> parent shelf",
            )
        ],
    )
    bound = _bound_query(registry, make_consumer, make_policy)
    untyped_traverse: Any = bound.traverse

    with raises_code(ValidationFailed, "INVALID_PARAMS"):
        untyped_traverse("parentShelf")


def test_bound_query_typed_traverse_refuses_missing_source(
    make_registry: RegistryFactory,
    make_consumer: ConsumerFactory,
    make_policy: PolicyFactory,
) -> None:
    registry = _functions_registry(make_registry)
    bound = _bound_query(registry, make_consumer, make_policy)
    untyped_traverse: Any = bound.traverse

    class _From:
        pass

    class _To:
        pass

    with raises_code(ValidationFailed, "INVALID_PARAMS"):
        untyped_traverse(LinkHandle("parentShelf", _From, _To))


# -- BoundQuery.aggregate_by --------------------------------------------------


def _item_registry_and_policy(
    make_registry: RegistryFactory, make_policy: PolicyFactory
) -> tuple[OntologyRegistry, ScopePolicy]:
    """A tiny "Item" domain, all scoped under one `org_id` (so a single
    consumer sees every group `aggregate_by` produces) -- deliberately
    separate from `_functions_registry()`'s single-property `Shelf`, which has no
    numeric field to aggregate."""
    registry = make_registry(
        object_types=[
            ObjectTypeDef(
                api_name="Item",
                display_name="Item",
                description="A generic item, for aggregate_by tests",
                layer="L0",
                properties=[
                    PropertyDef(name="id", type="str"),
                    PropertyDef(name="org_id", type="str", required=False),
                    PropertyDef(name="category", type="str", required=False),
                    PropertyDef(name="segment", type="str", required=False),
                    PropertyDef(name="value", type="int", required=False),
                ],
                primary_key="id",
            )
        ]
    )
    policy = make_policy(
        levels=["org"],
        rules={"Item": [DirectProperty(level="org", property_name="org_id")]},
        min_n=1,
    )
    return registry, policy


def _item_bound_query(
    registry: OntologyRegistry,
    policy: ScopePolicy,
    make_consumer: ConsumerFactory,
) -> BoundQuery:
    store = ObjectStore(registry)
    store.insert(
        "Item",
        {"id": "i1", "org_id": "org-1", "category": "A", "segment": "x", "value": 10},
        SRC,
    )
    store.insert(
        "Item",
        {"id": "i2", "org_id": "org-1", "category": "A", "segment": "y", "value": 100},
        SRC,
    )
    store.insert(
        "Item",
        {"id": "i3", "org_id": "org-1", "category": "B", "segment": "x", "value": 5},
        SRC,
    )
    query = GuardedQuery(store, registry, policy)
    consumer = make_consumer(
        actor_id="u1",
        role="Member",
        scope_level="org",
        scope_id="org-1",
        kind="human",
    )
    return BoundQuery(query, consumer)


def test_bound_query_aggregate_by_delegates_and_respects_where(
    make_registry: RegistryFactory,
    make_policy: PolicyFactory,
    make_consumer: ConsumerFactory,
) -> None:
    """Mutation receipt (pagination-hardening T3 review P1): dropping
    `where=` from `BoundQuery.aggregate_by`'s delegation to
    `GuardedQuery.aggregate_by` left every prior test green -- assert the
    FILTERED dict's actual values, not merely its shape, so a dropped/
    mis-passed `where=` widens the aggregate past the caller's filter and
    is caught here."""
    registry, policy = _item_registry_and_policy(make_registry, make_policy)
    bound = _item_bound_query(registry, policy, make_consumer)

    unfiltered = bound.aggregate_by("Item", "value", "category")
    assert unfiltered == {"A": pytest.approx(55.0), "B": pytest.approx(5.0)}

    filtered = bound.aggregate_by("Item", "value", "category", where={"segment": "x"})
    assert filtered == {"A": pytest.approx(10.0), "B": pytest.approx(5.0)}


def test_bound_query_aggregate_by_rejects_empty_group_by(
    make_registry: RegistryFactory,
    make_policy: PolicyFactory,
    make_consumer: ConsumerFactory,
) -> None:
    registry, policy = _item_registry_and_policy(make_registry, make_policy)
    bound = _item_bound_query(registry, policy, make_consumer)

    with raises_code(ValidationFailed, "INVALID_GROUP_BY"):
        bound.aggregate_by("Item", "value", "")


# -- BoundQuery.count_contributors: the DELEGATION must carry the gates ------
#
# Reviewer P0: the engine-layer pin in `tests/test_query.py` says nothing
# about this layer. Replacing this method's body with an unguarded
# `len(store.read_all(...))` left the whole suite green -- and BoundQuery is
# the surface a Function actually calls, so an ungated delegation here is
# the oracle reaching real callers. These tests assert behaviour THROUGH
# `BoundQuery`, never through `GuardedQuery`.


def _counting_registry(make_registry: RegistryFactory) -> OntologyRegistry:
    return make_registry(
        object_types=[
            ObjectTypeDef(
                api_name="Note",
                display_name="Note",
                description="A note pinned to a shelf",
                layer="L0",
                properties=[
                    PropertyDef(name="id", type="str"),
                    PropertyDef(name="shelf_id", type="str", required=False),
                ],
                primary_key="id",
            )
        ]
    )


def _counting_policy(make_policy: PolicyFactory) -> ScopePolicy:
    """min_n=3, and `Note` declares NO contributor_rules -- so this also
    exercises the row-count fallback across the delegation."""
    return make_policy(
        levels=["shelf"],
        rules={"Note": [DirectProperty(level="shelf", property_name="shelf_id")]},
        min_n=3,
    )


def _counting_bound_query(
    on_shelf_1: int,
    on_shelf_2: int = 0,
    *,
    make_registry: RegistryFactory,
    make_policy: PolicyFactory,
    make_consumer: ConsumerFactory,
) -> BoundQuery:
    registry = _counting_registry(make_registry)
    store = ObjectStore(registry)
    for i in range(on_shelf_1):
        store.insert("Note", {"id": f"s1-{i}", "shelf_id": "shelf-1"}, SRC)
    for i in range(on_shelf_2):
        store.insert("Note", {"id": f"s2-{i}", "shelf_id": "shelf-2"}, SRC)
    query = GuardedQuery(store, registry, _counting_policy(make_policy))
    return BoundQuery(
        query,
        make_consumer(
            actor_id="u1",
            role="Member",
            scope_level="shelf",
            scope_id="shelf-1",
            kind="human",
        ),
    )


def test_bound_query_count_contributors_releases_at_threshold(
    make_registry: RegistryFactory,
    make_policy: PolicyFactory,
    make_consumer: ConsumerFactory,
) -> None:
    assert (
        _counting_bound_query(
            on_shelf_1=3,
            make_registry=make_registry,
            make_policy=make_policy,
            make_consumer=make_consumer,
        ).count_contributors("Note")
        == 3
    )


def test_bound_query_count_contributors_refuses_below_min_n(
    make_registry: RegistryFactory,
    make_policy: PolicyFactory,
    make_consumer: ConsumerFactory,
) -> None:
    """The oracle pin at the layer a Function actually calls."""
    with raises_code(VisibilityError, "MIN_N_VIOLATION"):
        _counting_bound_query(
            on_shelf_1=2,
            make_registry=make_registry,
            make_policy=make_policy,
            make_consumer=make_consumer,
        ).count_contributors("Note")


def test_bound_query_count_contributors_does_not_count_out_of_scope_rows(
    make_registry: RegistryFactory,
    make_policy: PolicyFactory,
    make_consumer: ConsumerFactory,
) -> None:
    """2 visible + 5 invisible. An unscoped delegation would see 7, clear
    min_n=3, and hand back a count for a population this consumer cannot
    read -- so this fails loudly instead of returning 7 or 2."""
    with raises_code(VisibilityError, "MIN_N_VIOLATION"):
        _counting_bound_query(
            on_shelf_1=2,
            on_shelf_2=5,
            make_registry=make_registry,
            make_policy=make_policy,
            make_consumer=make_consumer,
        ).count_contributors("Note")


def test_bound_query_count_contributors_counts_only_the_visible_shelf(
    make_registry: RegistryFactory,
    make_policy: PolicyFactory,
    make_consumer: ConsumerFactory,
) -> None:
    """The release-direction twin of the test above: 3 visible, 5 invisible
    -> 3, never 8."""
    bq = _counting_bound_query(
        on_shelf_1=3,
        on_shelf_2=5,
        make_registry=make_registry,
        make_policy=make_policy,
        make_consumer=make_consumer,
    )
    assert bq.count_contributors("Note") == 3


def test_bound_query_count_contributors_honours_the_where_filter(
    make_registry: RegistryFactory,
    make_policy: PolicyFactory,
    make_consumer: ConsumerFactory,
) -> None:
    bq = _counting_bound_query(
        on_shelf_1=3,
        make_registry=make_registry,
        make_policy=make_policy,
        make_consumer=make_consumer,
    )
    with raises_code(VisibilityError, "MIN_N_VIOLATION"):
        bq.count_contributors("Note", where={"id": "s1-0"})
