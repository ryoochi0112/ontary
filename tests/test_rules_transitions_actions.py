"""Action path enforcement and rollback for authored declarations."""

from __future__ import annotations

from datetime import date
from enum import StrEnum
from typing import Any

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
from ontary.errors import PreconditionFailed, ValidationFailed
from ontary.meta import TransitionDef


class Status(StrEnum):
    PENDING = "pending"
    PAID = "paid"
    SHIPPED = "shipped"
    CANCELLED = "cancelled"


ontology = Ontology("orders", ["org"], min_n=1)


@ontology.object(layer="L0", scope="unscoped", owned=True)
class Order(OntologyObject):
    id: str = prop(primary_key=True)
    status: Status = prop(transitions=TransitionDef(
        initial=("pending",),
        moves={"pending": ("paid", "cancelled"), "paid": ("shipped", "cancelled"), "shipped": (), "cancelled": ()},
    ))
    paid_at: str | None = None
    placed_on: date | None = None


@ontology.object(layer="L0", scope="unscoped", owned=True)
class OptionalOrder(OntologyObject):
    id: str = prop(primary_key=True)
    status: Status | None = prop(default=None, transitions=TransitionDef(
        initial=("pending",),
        moves={"pending": ("paid", "cancelled"), "paid": ("shipped", "cancelled"), "shipped": (), "cancelled": ()},
    ))


@ontology.rule(Order, "shipped_needs_payment", message="payment required")
def shipped_needs_payment(order: Order) -> bool:
    assert type(order) is Order
    if order.placed_on is not None:
        assert type(order.placed_on) is date
    return order.status is not Status.SHIPPED or order.paid_at is not None


class CreateParams(ActionParams):
    status: Status


class MoveParams(ActionParams):
    order_id: str = target(Order)
    status: Status


class OptionalParams(ActionParams):
    pass


@ontology.action(CreateParams, target=Order, roles=["Clerk"], api_name="CreateOrder")
def create_order(ctx: ActionContext, params: CreateParams) -> dict[str, Any]:
    order = ctx.create(Order, id="o-2", status=params.status)
    return {"id": order.id}


@ontology.action(MoveParams, target=Order, roles=["Clerk"], api_name="MoveOrder")
def move_order(ctx: ActionContext, params: MoveParams) -> dict[str, Any]:
    ctx.create(Order, id="earlier", status=Status.PENDING)
    order = ctx.get(Order, params.order_id)
    assert order is not None
    order.status = params.status
    ctx.save(order)
    return {}


@ontology.action(OptionalParams, target=OptionalOrder, roles=["Clerk"], api_name="CreateOptional")
def create_optional(ctx: ActionContext, params: OptionalParams) -> dict[str, Any]:
    obj = ctx.create(OptionalOrder, id="optional")
    return {"id": obj.id}


def _client() -> tuple[ObjectStore, Any]:
    store = ObjectStore(ontology.registry)
    store.insert("Order", {"id": "o-1", "status": "pending"}, Source(source_system="seed"))
    consumer = Consumer(actor_id="clerk", role="Clerk", scope_level="org", scope_id="org-1", kind="human")
    return store, ontology.bind(store).for_consumer(consumer)


def test_action_create_rejects_non_initial_status_and_audits() -> None:
    store, client = _client()
    with raises_code(PreconditionFailed, "TRANSITION_NOT_ALLOWED"):
        client.execute(CreateParams(status=Status.PAID))
    assert store.read_current("Order", "o-2") is None
    audit = store.audit_entries()[-1]
    assert audit.outcome == "error" and audit.error_code == "TRANSITION_NOT_ALLOWED" and audit.writes == []


def test_action_move_refusal_uses_golden_message_and_rolls_back() -> None:
    store, client = _client()
    message = "Order 'o-1': status cannot move from 'pending' to 'shipped'; allowed from 'pending': ['paid', 'cancelled']"
    with raises_code(PreconditionFailed, "TRANSITION_NOT_ALLOWED") as exc:
        client.execute(MoveParams(order_id="o-1", status=Status.SHIPPED))
    assert str(exc.value) == message
    assert store.read_current("Order", "earlier") is None
    assert store.read_current("Order", "o-1").payload["status"] == "pending"  # type: ignore[union-attr]
    audit = store.audit_entries()[-1]
    assert audit.outcome == "error" and audit.error_code == "TRANSITION_NOT_ALLOWED" and audit.writes == []


def test_captured_create_can_leave_optional_governed_property_unset() -> None:
    store, client = _client()
    assert client.execute(OptionalParams()) == {"id": "optional"}
    row = store.read_current("OptionalOrder", "optional")
    assert row is not None and row.payload.get("status") is None


def test_rule_refusal_rolls_back_prior_action_write_and_audits() -> None:
    store, client = _client()
    store.update("Order", "o-1", {"status": "paid", "placed_on": date(2026, 9, 28)}, Source(source_system="seed"))
    with raises_code(ValidationFailed, "RULE_VIOLATED") as exc:
        client.execute(MoveParams(order_id="o-1", status=Status.SHIPPED))
    assert "shipped_needs_payment" in str(exc.value)
    assert store.read_current("Order", "earlier") is None
    row = store.read_current("Order", "o-1")
    assert row is not None and row.payload["status"] == "paid"
    assert row.payload["placed_on"] == "2026-09-28"
    audit = store.audit_entries()[-1]
    assert audit.outcome == "error" and audit.error_code == "RULE_VIOLATED" and audit.writes == []
