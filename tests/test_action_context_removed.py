"""Removal of the string ActionContext surface in 0.18.0."""

from __future__ import annotations

from typing import Any, cast

import pytest
from conftest import raises_code

from ontary import (
    ActionContext,
    ActionParams,
    Consumer,
    ObjectStore,
    Ontology,
    OntologyObject,
    Source,
    prop,
    target,
)
from ontary.actions import ActionExecutor
from ontary.errors import ValidationFailed
from ontary.model import LinkHandle
from ontary.scope import ScopePolicy

ontology = Ontology("removed-ctx", scope_levels=["org"], min_n=1)


@ontology.object(layer="L0", scope="unscoped", owned=True)
class Customer(OntologyObject):
    id: str = prop(primary_key=True)
    name: str


@ontology.object(layer="L0", scope="unscoped", owned=True)
class Order(OntologyObject):
    id: str = prop(primary_key=True)
    status: str


orderOf: LinkHandle[Order, Customer] = ontology.link(
    "orderOf", Order, Customer, "MANY_TO_ONE", owned=True
)


class OrderRef(ActionParams):
    order_id: str = target(Order)


class InsertParams(ActionParams):
    customer_id: str = target(Customer)


@ontology.action(InsertParams, target=Customer, roles=["Clerk"], api_name="OldInsert")
def _old_insert(ctx: ActionContext, params: InsertParams) -> dict[str, Any]:
    ctx.insert("Order", {"id": "o2", "status": "new"})  # type: ignore[attr-defined]
    return {}


@ontology.action(OrderRef, target=Order, roles=["Clerk"], api_name="OldRetire")
def _old_retire(ctx: ActionContext, params: OrderRef) -> dict[str, Any]:
    ctx.create(Order, id="transient", status="new")
    ctx.retire(cast(Any, "Order"), params.order_id)
    return {}


class UnlinkParams(OrderRef):
    pass


@ontology.action(UnlinkParams, target=Order, roles=["Clerk"], api_name="OldUnlink")
def _old_unlink(ctx: ActionContext, params: UnlinkParams) -> dict[str, Any]:
    ctx.create(Order, id="transient", status="new")
    ctx.unlink(cast(Any, "orderOf"), params.order_id, "c1")
    return {}


class RetireParams(OrderRef):
    pass


@ontology.action(RetireParams, target=Order, roles=["Clerk"], api_name="TypedRetire")
def _typed_retire(ctx: ActionContext, params: RetireParams) -> dict[str, Any]:
    order = ctx.get(Order, params.order_id)
    assert order is not None
    ctx.retire(order)
    return {}


class UnlinkTypedParams(OrderRef):
    pass


@ontology.action(UnlinkTypedParams, target=Order, roles=["Clerk"], api_name="TypedUnlink")
def _typed_unlink(ctx: ActionContext, params: UnlinkTypedParams) -> dict[str, Any]:
    ctx.unlink(orderOf, params.order_id, "c1")
    return {}


ontology.validate()


@pytest.fixture
def runtime() -> tuple[ActionExecutor, ObjectStore, Consumer]:
    store = ObjectStore(ontology.registry)
    seed = Source(source_system="seed")
    store.insert("Customer", {"id": "c1", "name": "Ada"}, seed)
    store.insert("Order", {"id": "o1", "status": "new"}, seed)
    store.create_link("orderOf", "o1", "c1")
    executor = ActionExecutor(
        store,
        ontology.registry,
        ScopePolicy(levels=["org"], unscoped_types={"Customer", "Order"}, min_n=1),
    )
    executor._register("OldInsert", _old_insert, InsertParams)
    executor._register("OldRetire", _old_retire, OrderRef)
    executor._register("OldUnlink", _old_unlink, UnlinkParams)
    executor._register("TypedRetire", _typed_retire, RetireParams)
    executor._register("TypedUnlink", _typed_unlink, UnlinkTypedParams)
    consumer = Consumer(
        actor_id="clerk-1", role="Clerk", scope_level="org", scope_id="org-1", kind="human"
    )
    return executor, store, consumer


@pytest.mark.parametrize(
    "name", ["insert", "update", "create_link", "read_current", "read_all", "links_from", "links_to"]
)
def test_removed_member_is_absent(name: str) -> None:
    assert not hasattr(ActionContext, name)


def test_removed_insert_fails_inside_action_and_is_audited(
    runtime: tuple[ActionExecutor, ObjectStore, Consumer],
) -> None:
    executor, store, consumer = runtime
    with pytest.raises(AttributeError, match="insert"):
        executor.execute(consumer, "OldInsert", {"customer_id": "c1"})
    assert store.read_current("Order", "o2") is None
    assert store.audit_entries()[-1].outcome == "error"


@pytest.mark.parametrize(
    ("action", "typed_form"),
    [("OldRetire", "ctx.retire(obj)"), ("OldUnlink", "ctx.unlink(handle, from_, to)")],
)
def test_string_argument_fails_without_writes(
    runtime: tuple[ActionExecutor, ObjectStore, Consumer], action: str, typed_form: str
) -> None:
    executor, store, consumer = runtime
    with raises_code(ValidationFailed, "INVALID_PARAMS") as exc_info:
        executor.execute(consumer, action, {"order_id": "o1"})
    assert typed_form in str(exc_info.value)
    if action == "OldRetire":
        assert "ctx.retire(Cls, obj_id)" in str(exc_info.value)
    assert store.read_current("Order", "o1") is not None
    assert store.read_current("Order", "transient") is None
    assert store.links_from("orderOf", "o1") == ["c1"]
    assert store.audit_entries()[-1].outcome == "error"


def test_typed_retire_cascades_links(
    runtime: tuple[ActionExecutor, ObjectStore, Consumer],
) -> None:
    executor, store, consumer = runtime
    executor.execute(consumer, "TypedRetire", {"order_id": "o1"})
    assert store.read_current("Order", "o1") is None
    assert store.links_from("orderOf", "o1") == []


def test_typed_unlink_closes_one_link(
    runtime: tuple[ActionExecutor, ObjectStore, Consumer],
) -> None:
    executor, store, consumer = runtime
    executor.execute(consumer, "TypedUnlink", {"order_id": "o1"})
    assert store.read_current("Order", "o1") is not None
    assert store.links_from("orderOf", "o1") == []
