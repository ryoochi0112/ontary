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
    CapabilityDef,
    Cardinality,
    FunctionDef,
    LinkTypeDef,
    ObjectTypeDef,
    OntologyRegistry,
    PropertyDef,
)
from ontary.model import LinkHandle
from ontary.query import (
    _AUTHOR_DISPATCH,
    DEFAULT_READ_LIMIT,
    GuardedQuery,
    Page,
    TypedPage,
)
from ontary.scope import DirectProperty, ScopePolicy, SelfScope, ViaLink
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
            ),
            FunctionDef(
                api_name="countShelvesWithReach",
                description="counts shelves and may reach outside",
                input_description="none",
                output_description="int",
                capabilities=["clock"],
            ),
        ],
        capabilities=[
            CapabilityDef(api_name="clock", description="reads the wall clock")
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


def _reverse_bound_query(
    make_registry: RegistryFactory,
    make_consumer: ConsumerFactory,
    make_policy: PolicyFactory,
) -> BoundQuery:
    registry = make_registry(
        object_types=[
            ObjectTypeDef(
                api_name="Source",
                display_name="Source",
                description="A reverse-traversal source",
                layer="L0",
                properties=[PropertyDef(name="id", type="str")],
                primary_key="id",
            ),
            ObjectTypeDef(
                api_name="Target",
                display_name="Target",
                description="A reverse-traversal target",
                layer="L0",
                properties=[PropertyDef(name="id", type="str")],
                primary_key="id",
            ),
        ],
        link_types=[
            LinkTypeDef(
                api_name="related",
                from_type="Source",
                to_type="Target",
                cardinality=Cardinality.MANY_TO_MANY,
                description="Source -> related targets",
            )
        ],
    )
    policy = make_policy(
        levels=["scope"],
        unscoped_types={"Source", "Target"},
        rules={},
        min_n=1,
    )
    store = ObjectStore(registry)
    for object_type, object_id in (
        ("Source", "source-a"),
        ("Source", "source-c"),
        ("Target", "target-b"),
        ("Target", "target-d"),
    ):
        store.insert(object_type, {"id": object_id}, SRC)
    store.create_link("related", "source-a", "target-b")
    store.create_link("related", "source-c", "target-b")
    store.create_link("related", "source-a", "target-d")
    query = GuardedQuery(store, registry, policy)
    return BoundQuery(
        query,
        make_consumer(
            actor_id="u1",
            role="Member",
            scope_level="scope",
            scope_id="all",
            kind="human",
        ),
        registry,
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
        return len(query.list("Shelf", limit=None))

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


def test_dispatch_never_elevates_the_callers_bound_query(
    make_registry: RegistryFactory,
    make_consumer: ConsumerFactory,
    make_policy: PolicyFactory,
) -> None:
    """A dispatch's authorities land on a `BoundQuery` the caller does not
    hold -- observed FROM INSIDE the handler, while the call is live.

    `call` used to assign the declared-capability set onto the caller's own
    object and restore it in a `finally`. That is what the mid-call
    assertions below could not have passed: for the duration of the handler
    the caller's object carried the function's authority, reachable from a
    second thread or from any caller code the handler re-enters. Restoring
    afterwards narrows the window; it does not close it.

    Author provenance now travels the same way, which is why it had to move
    here rather than onto `BoundQuery.__init__`: a constructor argument is
    an authority the caller supplies, and the caller keeps the object.
    """
    registry = _functions_registry(make_registry)
    functions = FunctionRegistry(registry)
    caller_bound = _bound_query(registry, make_consumer, make_policy)
    seen: dict[str, Any] = {}

    def _handler(query: BoundQuery, params: dict[str, Any]) -> int:
        seen["dispatched"] = query
        # The caller's object, mid-dispatch: neither authority has touched it.
        seen["caller_grant_during"] = caller_bound._author_dispatch
        seen["caller_caps_during"] = caller_bound._declared_capabilities
        return 0

    functions.register("countShelvesWithReach", _handler)
    functions.call("countShelvesWithReach", caller_bound, {})

    dispatched = seen["dispatched"]
    assert dispatched is not caller_bound
    assert dispatched._author_dispatch is _AUTHOR_DISPATCH
    assert dispatched._declared_capabilities == frozenset({"clock"})

    assert seen["caller_grant_during"] is None
    assert seen["caller_caps_during"] == frozenset()
    assert caller_bound._author_dispatch is None
    assert caller_bound._declared_capabilities == frozenset()


def _bulk_bound_query(
    make_registry: RegistryFactory,
    make_consumer: ConsumerFactory,
    make_policy: PolicyFactory,
    n_rows: int,
) -> tuple[BoundQuery, FunctionRegistry, int]:
    """A Function-reachable selection of `n_rows` rows, all visible.

    `_functions_registry`'s Shelf is `SelfScope`-routed, so a shelf-1 consumer
    sees exactly one Shelf however many are inserted -- it structurally cannot
    build a large selection. This adds a `DirectProperty`-routed type so every
    seeded row lands in the consumer's scope, which is the fixture the
    truncation pin needs and the reason it did not exist.
    """
    registry = _functions_registry(make_registry)
    registry.register_object_type(
        ObjectTypeDef(
            api_name="Reading",
            display_name="Reading",
            description="a reading on a shelf",
            layer="L0",
            properties=[
                PropertyDef(name="id", type="str"),
                PropertyDef(name="shelf_id", type="str", required=False),
                PropertyDef(name="score", type="float", required=False),
            ],
            primary_key="id",
        )
    )
    registry.register_function(
        FunctionDef(
            api_name="meanScore",
            description="mean score over the selection",
            input_description="none",
            output_description="float",
        )
    )
    policy = _functions_policy(make_policy)
    policy.rules = {
        **policy.rules,
        "Reading": [DirectProperty(level="shelf", property_name="shelf_id")],
    }
    store = ObjectStore(registry)
    store.insert("Shelf", {"id": "shelf-1"}, SRC)
    for i in range(n_rows):
        store.insert(
            "Reading",
            {"id": f"r{i}", "shelf_id": "shelf-1", "score": float(i)},
            SRC,
        )
    bound = BoundQuery(
        GuardedQuery(store, registry, policy),
        make_consumer(
            actor_id="u1",
            role="Member",
            scope_level="shelf",
            scope_id="shelf-1",
            kind="human",
        ),
    )
    return bound, FunctionRegistry(registry), n_rows


def test_function_list_reduces_over_the_whole_selection_not_one_page(
    make_registry: RegistryFactory,
    make_consumer: ConsumerFactory,
    make_policy: PolicyFactory,
) -> None:
    """A declared Function reducing over `query.list()` sees every visible row,
    and agrees with the engine's own reductions.

    The input no fixture could previously construct: **a Function over a
    selection larger than `DEFAULT_READ_LIMIT`.** `DEFAULT_READ_LIMIT` appears
    in `tests/` only in `test_pagination.py`, which drives `gq.get_objects`,
    never a `BoundQuery` inside a dispatch -- so a green suite coexisted with
    `count` and `list` disagreeing by 200 rows inside one Function.

    `list` inheriting the CONSUMER surface's bound is what made them disagree:
    it returned a 1000-row `Page` while `count`/`aggregate` read unbounded
    through `Store.read_all`. A Function reporting "mean X over N rows" from
    the two reported a self-inconsistent pair, silently. Asserting all three
    together is what pins that they cannot drift apart again.
    """
    n_rows = DEFAULT_READ_LIMIT + 200
    bound, functions, n_rows = _bulk_bound_query(
        make_registry, make_consumer, make_policy, n_rows
    )
    seen: dict[str, Any] = {}

    def _mean(query: BoundQuery, params: dict[str, Any]) -> float:
        rows = query.list("Reading")
        seen["rows"] = len(rows)
        seen["count"] = query.count("Reading")
        return sum(r.payload["score"] for r in rows) / len(rows)

    functions.register("meanScore", _mean)
    mean = functions.call("meanScore", bound, {})

    assert seen["rows"] == n_rows
    assert seen["count"] == n_rows
    assert mean == pytest.approx((n_rows - 1) / 2)


def test_function_list_still_pages_when_a_limit_is_asked_for(
    make_registry: RegistryFactory,
    make_consumer: ConsumerFactory,
    make_policy: PolicyFactory,
) -> None:
    """Unbounded is the DEFAULT, not the only option -- paging is still there
    for a Function that wants it, on the same contract as the consumer surface.

    Without this, the fix above is indistinguishable from deleting paging from
    the author surface altogether.
    """
    bound, _functions, _n = _bulk_bound_query(
        make_registry, make_consumer, make_policy, DEFAULT_READ_LIMIT + 200
    )

    page = bound.list("Reading", limit=10)
    assert isinstance(page, Page)
    assert len(page.items) == 10
    assert page.next_cursor is not None


def test_page_refuses_to_be_walked_as_a_sequence(
    make_registry: RegistryFactory,
    make_consumer: ConsumerFactory,
    make_policy: PolicyFactory,
) -> None:
    """`for row in page` used to yield `('items', [...])` and
    `('next_cursor', ...)` from the inherited `BaseModel.__iter__`, failing
    later as `AttributeError: 'tuple' object has no attribute 'payload'` at
    whatever touched the row -- an error naming neither the page nor the fix.

    Pinned on the count too: an assertion that merely `raises` would also pass
    against a `Page` that yielded its two field pairs and blew up downstream,
    which is exactly the behaviour being replaced.
    """
    bound, _functions, _n = _bulk_bound_query(
        make_registry, make_consumer, make_policy, 5
    )
    page = bound.list("Reading", limit=2)

    with raises_code(ValidationFailed, "PAGE_NOT_ITERABLE"):
        list(page)
    with raises_code(ValidationFailed, "PAGE_NOT_ITERABLE"):
        for _row in page:
            pass
    assert len(page.items) == 2

    # The refusal is scoped to walking it: a Page is still an ordinary frozen
    # model. `model_dump`/`==`/`model_copy` do not route through `__iter__`,
    # and a refusal that broke them would be a worse bug than the one fixed.
    assert page.model_dump()["next_cursor"] == page.next_cursor
    assert page == page.model_copy()


def test_typed_page_refuses_to_be_walked_on_its_own_name() -> None:
    """`TypedPage` carries the same inherited iteration and needs its own
    assertion.

    Found by mutation: patching only `TypedPage.__iter__` to yield `.items`
    left the `Page` pin above green, because that pin exercises the string
    form and never touches this class. Two classes, two refusals, two names.
    """
    typed: TypedPage[Any] = TypedPage(items=[], next_cursor=None)

    with raises_code(ValidationFailed, "PAGE_NOT_ITERABLE"):
        list(typed)
    assert typed.items == []


def test_page_refuses_to_be_measured_or_indexed(
    make_registry: RegistryFactory,
    make_consumer: ConsumerFactory,
    make_policy: PolicyFactory,
) -> None:
    """`PAGE_NOT_ITERABLE` promises the refusal for `len(page)` and
    `page[0]` too, not just `for row in page`.

    Only `__iter__` was overridden, so both fell through to `BaseModel`'s
    bare `TypeError` -- the exact naming-nothing failure this code exists to
    replace, under an error message the catalog says does not happen. The
    iteration pins above cannot see it: they never measure or subscript.
    """
    bound, _functions, _n = _bulk_bound_query(
        make_registry, make_consumer, make_policy, 5
    )
    page = bound.list("Reading", limit=2)
    typed: TypedPage[Any] = TypedPage(items=[], next_cursor=None)

    for measured in (page, typed):
        with raises_code(ValidationFailed, "PAGE_NOT_ITERABLE"):
            len(measured)
        with raises_code(ValidationFailed, "PAGE_NOT_ITERABLE"):
            measured[0]
        with raises_code(ValidationFailed, "PAGE_NOT_ITERABLE"):
            measured[0:1]

    # Measuring is refused; being an ordinary frozen model is not. Truthiness
    # would otherwise fall through to the new `__len__` and start raising --
    # a behaviour change no error code promises.
    assert bool(page) is True
    assert bool(typed) is True
    assert len(page.items) == 2


# -- BoundQuery read-only surface --------------------------------------------


def test_bound_query_exposes_only_the_guarded_read_methods() -> None:
    public_names = {name for name in dir(BoundQuery) if not name.startswith("_")}
    # `capability` is a READ of the outside world (M5, AC6). A Function still
    # cannot write: no store handle, and no effect dispatchers either (AC4/AC5b).
    assert public_names == {
        "capability",
        "count",
        "exists",
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
    objs = bound.list("Shelf", limit=None)
    assert [o.payload["id"] for o in objs] == ["shelf-1"]


def test_bound_query_reverse_flag_preserves_link_direction(
    make_registry: RegistryFactory,
    make_consumer: ConsumerFactory,
    make_policy: PolicyFactory,
) -> None:
    bound = _reverse_bound_query(make_registry, make_consumer, make_policy)

    forward = bound.traverse("related", "source-a")
    reverse = bound.traverse("related", "target-b", reverse=True)
    assert {row.payload["id"] for row in forward} == {"target-b", "target-d"}
    assert {row.payload["id"] for row in reverse} == {"source-a", "source-c"}


def test_bound_query_via_keyword_removed(
    make_registry: RegistryFactory,
    make_consumer: ConsumerFactory,
    make_policy: PolicyFactory,
) -> None:
    """AC19 pins removal of the `BoundQuery.traverse` `via=` keyword."""
    bound = _reverse_bound_query(make_registry, make_consumer, make_policy)
    untyped_traverse: Any = bound.traverse

    with pytest.raises(TypeError, match="unexpected keyword argument 'via'"):
        untyped_traverse("source-a", via=LinkHandle("related", object, object))


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


def test_bound_query_aggregate_functions_delegate_without_losing_func(
    make_registry: RegistryFactory,
    make_policy: PolicyFactory,
    make_consumer: ConsumerFactory,
) -> None:
    registry, policy = _item_registry_and_policy(make_registry, make_policy)
    bound = _item_bound_query(registry, policy, make_consumer)

    assert bound.aggregate("Item", "value", func="count") == 3
    assert bound.aggregate("Item", "value", func="min") == pytest.approx(5.0)
    assert bound.aggregate_by("Item", "value", "category", func="sum") == {
        "A": pytest.approx(110.0),
        "B": pytest.approx(5.0),
    }


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


def _erased_parent_bound_query(
    make_registry: RegistryFactory,
    make_policy: PolicyFactory,
    make_consumer: ConsumerFactory,
) -> tuple[BoundQuery, ObjectStore]:
    """`Reading -> Reader -> Org`, min_n=3, with one reader erasable.

    Every scope hop is a `ViaLink`, so resolving a `Reading` has to walk
    through the `Reader` row -- the seam an erased parent has to stay
    invisible at.
    """
    registry = make_registry(
        object_types=[
            ObjectTypeDef(
                api_name="Org",
                display_name="Org",
                description="An organisation",
                layer="L0",
                properties=[PropertyDef(name="id", type="str")],
                primary_key="id",
            ),
            ObjectTypeDef(
                api_name="Reader",
                display_name="Reader",
                description="A reader, erasable on request",
                layer="L0",
                properties=[PropertyDef(name="id", type="str")],
                primary_key="id",
            ),
            ObjectTypeDef(
                api_name="Reading",
                display_name="Reading",
                description="One metered reading",
                layer="L0",
                properties=[
                    PropertyDef(name="id", type="str"),
                    PropertyDef(name="value", type="float"),
                ],
                primary_key="id",
            ),
        ],
        link_types=[
            LinkTypeDef(
                api_name="inOrg",
                from_type="Reader",
                to_type="Org",
                cardinality=Cardinality.MANY_TO_MANY,
                description="Reader -> Org",
            ),
            LinkTypeDef(
                api_name="byReader",
                from_type="Reading",
                to_type="Reader",
                cardinality=Cardinality.MANY_TO_MANY,
                description="Reading -> Reader",
            ),
        ],
    )
    policy = make_policy(
        levels=["org"],
        rules={
            "Org": [SelfScope(level="org")],
            "Reader": [ViaLink(link_api_name="inOrg", direction="from", parent_type="Org")],
            "Reading": [
                ViaLink(link_api_name="byReader", direction="from", parent_type="Reader")
            ],
        },
        contributor_rules={
            "Reader": [SelfScope(level="org")],
            "Reading": [
                ViaLink(link_api_name="byReader", direction="from", parent_type="Reader")
            ],
        },
        min_n=3,
    )
    store = ObjectStore(registry)
    store.insert("Org", {"id": "org-1"}, SRC)
    for reader_id in ("r-a", "r-b", "r-gone"):
        store.insert("Reader", {"id": reader_id}, SRC)
        store.create_link("inOrg", reader_id, "org-1")
    for reading_id, reader_id, value in (
        ("rd-a1", "r-a", 10.0),
        ("rd-a2", "r-a", 20.0),
        ("rd-b1", "r-b", 30.0),
        ("rd-g1", "r-gone", 40.0),
        ("rd-g2", "r-gone", 50.0),
    ):
        store.insert("Reading", {"id": reading_id, "value": value}, SRC)
        store.create_link("byReader", reading_id, reader_id)
    query = GuardedQuery(store, registry, policy)
    bound = BoundQuery(
        query,
        make_consumer(
            actor_id="u1",
            role="Member",
            scope_level="org",
            scope_id="org-1",
            kind="human",
        ),
    )
    return bound, store


def test_erased_scope_parent_does_not_resurrect_its_children_for_readers(
    make_registry: RegistryFactory,
    make_policy: PolicyFactory,
    make_consumer: ConsumerFactory,
) -> None:
    """An erased `Reader` must stop resolving scope for its `Reading` rows.

    `erase_object_content` retires the subject's row through the STORE verb,
    which does not cascade link closure -- so the `inOrg` link outlives the
    erasure. A scope resolver that reads the NEWEST row instead of the live
    one therefore climbs an erased parent straight back to `org-1`, and the
    two readings that should have gone dark come back into the visible set.

    That is not a cosmetic count: it carries the population past min_n and
    releases an aggregate computed over the erased subject's own rows --
    `docs/compatibility.md:59`'s "a refusal becomes permissive", in the
    direction that matters most for governance.
    """
    bound, store = _erased_parent_bound_query(
        make_registry, make_policy, make_consumer
    )
    assert bound.count("Reading") == 5  # all five in scope while r-gone is live

    store.erase_object_content("Reader", "r-gone")

    assert bound.count("Reading") == 3  # r-gone's two readings go dark
    with raises_code(VisibilityError, "MIN_N_VIOLATION"):
        bound.count_contributors("Reading")
    with raises_code(VisibilityError, "MIN_N_VIOLATION"):
        bound.aggregate("Reading", "value", func="mean")


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


def test_bound_query_count_and_exists_use_visible_operator_selection_without_min_n(
    make_registry: RegistryFactory,
    make_policy: PolicyFactory,
    make_consumer: ConsumerFactory,
) -> None:
    bq = _counting_bound_query(
        on_shelf_1=3,
        on_shelf_2=5,
        make_registry=make_registry,
        make_policy=make_policy,
        make_consumer=make_consumer,
    )

    assert bq.count("Note", where={"id": {"contains": "s1-0"}}) == 1
    assert bq.exists("Note", where={"id": {"contains": "s1-0"}}) is True
    assert bq.exists("Note", where={"id": {"contains": "missing"}}) is False


def test_bound_query_count_and_exists_return_empty_for_scoped_away_rows(
    make_registry: RegistryFactory,
    make_policy: PolicyFactory,
    make_consumer: ConsumerFactory,
) -> None:
    bq = _counting_bound_query(
        on_shelf_1=0,
        on_shelf_2=5,
        make_registry=make_registry,
        make_policy=make_policy,
        make_consumer=make_consumer,
    )

    assert bq.count("Note") == 0
    assert bq.exists("Note") is False
