"""`ActionContext` keys objects by `canonical_id`, not `str()` (#242).

A `(str, Enum)` mixin member stringifies as `'Id.O1'`, but every store keys
the object by its value `'o1'` (#91). An object whose primary-key attribute
holds such a member -- one built with `model_construct`, or one whose key a
handler re-assigned to the equal member -- must still address `'o1'` when
the context links, unlinks, traverses, retires, saves, or emits about it.
"""

from __future__ import annotations

from collections.abc import Callable
from enum import Enum
from typing import Any

import pytest

from ontary import ActionContext, ActionParams, Ontology, OntologyObject, Source, prop
from ontary.model import Event
from ontary.store.inmemory import InMemoryStore
from ontary.testing import SequentialIds, consumer


class Id(str, Enum):
    O1 = "o1"
    C1 = "c1"


ontology = Ontology("canonical-ctx-ids", scope_levels=["org"], min_n=1)


@ontology.object(layer="L0", scope="unscoped", owned=True)
class Customer(OntologyObject):
    id: str = prop(primary_key=True)
    name: str


@ontology.object(layer="L0", scope="unscoped", owned=True)
class Order(OntologyObject):
    id: str = prop(primary_key=True)
    status: str


orderOf = ontology.link("orderOf", Order, Customer, "MANY_TO_ONE", owned=True)


@ontology.event()
class OrderTouched(Event):
    note: str


Handler = Callable[[ActionContext], dict[str, Any]]
_HANDLER: list[Handler] = []


class NoParams(ActionParams):
    pass


@ontology.action(NoParams, target=Order, roles=["Clerk"], api_name="Run", emits=[OrderTouched])
def _run(ctx: ActionContext, params: NoParams) -> dict[str, Any]:
    return _HANDLER[0](ctx)


ontology.validate()


@pytest.fixture
def store() -> InMemoryStore:
    store = InMemoryStore(ontology.registry)
    source = Source(source_system="seed")
    store.insert("Customer", {"id": "c1", "name": "Ada"}, source)
    store.insert("Order", {"id": "o1", "status": "new"}, source)
    return store


def _execute(store: InMemoryStore, handler: Handler) -> dict[str, Any]:
    _HANDLER[:] = [handler]
    client = ontology.bind(store, id_factory=SequentialIds("inv")).for_consumer(
        consumer(role="Clerk", kind="human")
    )
    return client.execute(NoParams())


def _constructed_order() -> Order:
    return Order.model_construct(id=Id.O1, status="new")


def _constructed_customer() -> Customer:
    return Customer.model_construct(id=Id.C1, name="Ada")


def test_link_and_unlink_key_constructed_endpoints_by_value(store: InMemoryStore) -> None:
    def link(ctx: ActionContext) -> dict[str, Any]:
        ctx.link(orderOf, _constructed_order(), _constructed_customer())
        return {}

    _execute(store, link)
    assert store.links_from("orderOf", "o1") == ["c1"]

    def unlink(ctx: ActionContext) -> dict[str, Any]:
        ctx.unlink(orderOf, _constructed_order(), _constructed_customer())
        return {}

    _execute(store, unlink)
    assert store.links_from("orderOf", "o1") == []


def test_traverse_keys_a_constructed_anchor_by_value(store: InMemoryStore) -> None:
    store.create_link("orderOf", "o1", "c1")
    seen: list[str] = []

    def traverse(ctx: ActionContext) -> dict[str, Any]:
        seen.extend(c.id for c in ctx.traverse(orderOf, _constructed_order()))
        return {}

    _execute(store, traverse)
    assert seen == ["c1"]


def test_traverse_many_keys_constructed_anchors_by_value(store: InMemoryStore) -> None:
    store.create_link("orderOf", "o1", "c1")
    seen: dict[str, list[str]] = {}

    def traverse_many(ctx: ActionContext) -> dict[str, Any]:
        result = ctx.traverse_many(orderOf, [_constructed_order()])
        seen.update({anchor: [c.id for c in found] for anchor, found in result.items()})
        return {}

    _execute(store, traverse_many)
    assert seen == {"o1": ["c1"]}


def test_retire_keys_a_constructed_object_by_value(store: InMemoryStore) -> None:
    def retire(ctx: ActionContext) -> dict[str, Any]:
        ctx.retire(_constructed_order())
        return {}

    _execute(store, retire)
    assert store.read_current("Order", "o1") is None


def test_a_second_save_after_an_equal_enum_key_reassignment_updates_the_stored_row(
    store: InMemoryStore,
) -> None:
    # The first save stores the reassigned key in the context's snapshot; the
    # second save then keys its update by that snapshot.
    def save_twice(ctx: ActionContext) -> dict[str, Any]:
        order = ctx.get(Order, "o1")
        assert order is not None
        order.id = Id.O1
        order.status = "packed"
        ctx.save(order)
        order.status = "shipped"
        ctx.save(order)
        return {}

    _execute(store, save_twice)
    current = store.read_current("Order", "o1")
    assert current is not None
    assert current.payload["status"] == "shipped"


def test_emit_about_an_equal_enum_key_names_the_stored_subject(store: InMemoryStore) -> None:
    def emit(ctx: ActionContext) -> dict[str, Any]:
        order = ctx.get(Order, "o1")
        assert order is not None
        order.id = Id.O1
        ctx.emit(OrderTouched(note="seen"), about=order)
        return {}

    _execute(store, emit)
    events = store.audit_entries()[0].events
    assert [(e.about_type, e.about_id) for e in events] == [("Order", "o1")]
