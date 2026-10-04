"""Event assertions in the eager scenario helper (#47)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from enum import Enum

import pytest
from pydantic import BaseModel

from ontary import ActionContext, ActionParams, Ontology, OntologyObject, prop, target
from ontary.errors import ValidationFailed
from ontary.model import Event
from ontary.testing import consumer, make_store, scenario

OPERATOR = consumer(role="Operator")
VIEWER = consumer(role="Viewer")


class DeliveryState(Enum):
    SHIPPED = "shipped"


class DeliveryDetail(BaseModel):
    state: DeliveryState
    note: str | None = None


@dataclass
class EventDomain:
    ontology: Ontology
    order: type[OntologyObject]
    other_subject: type[OntologyObject]
    shipped: type[Event]
    packed: type[Event]
    ready: type[Event]
    rich: type[Event]
    ship_params: type[ActionParams]
    noop_params: type[ActionParams]
    ready_params: type[ActionParams]
    rich_params: type[ActionParams]


@pytest.fixture
def domain() -> EventDomain:
    ontology = Ontology("scenario-events", scope_levels=["org"], min_n=1)

    @ontology.object(layer="L0", scope="unscoped", owned=True)
    class Order(OntologyObject):
        id: str = prop(primary_key=True)

    @ontology.object(layer="L0", scope="unscoped", owned=True)
    class OtherSubject(OntologyObject):
        id: str = prop(primary_key=True)

    @ontology.event()
    class OrderShipped(Event):
        carrier: str

    @ontology.event()
    class OrderPacked(Event):
        count: int

    @ontology.event()
    class OrderReady(Event):
        carrier: str

    @ontology.event()
    class DeliveryWindow(Event):
        day: date
        dispatched_at: datetime
        detail: DeliveryDetail

    class ShipOrder(ActionParams):
        order_id: str = target(Order)

    @ontology.action(
        ShipOrder,
        target=Order,
        roles=["Operator"],
        emits=[OrderShipped, OrderPacked, OrderReady],
    )
    def ship(ctx: ActionContext, _params: ShipOrder) -> dict[str, object]:
        ctx.emit(OrderShipped(carrier="yamato"))
        ctx.emit(OrderPacked(count=2))
        ctx.emit(OrderReady(carrier="yamato"))
        return {}

    class DoNothing(ActionParams):
        order_id: str = target(Order)

    @ontology.action(DoNothing, target=Order, roles=["Operator"])
    def do_nothing(_ctx: ActionContext, _params: DoNothing) -> dict[str, object]:
        return {}

    class AnnounceReady(ActionParams):
        order_id: str = target(Order)

    @ontology.action(
        AnnounceReady,
        target=Order,
        roles=["Operator"],
        emits=[OrderReady],
    )
    def announce_ready(ctx: ActionContext, _params: AnnounceReady) -> dict[str, object]:
        ctx.emit(OrderReady(carrier="yamato"))
        return {}

    class EmitRichEvent(ActionParams):
        order_id: str = target(Order)

    @ontology.action(
        EmitRichEvent,
        target=Order,
        roles=["Operator"],
        emits=[DeliveryWindow],
    )
    def emit_rich(ctx: ActionContext, _params: EmitRichEvent) -> dict[str, object]:
        ctx.emit(DeliveryWindow(
            day=date(2026, 10, 4),
            dispatched_at=datetime(2026, 10, 4, 9, tzinfo=timezone.utc),
            detail=DeliveryDetail(state=DeliveryState.SHIPPED),
        ))
        return {}

    return EventDomain(
        ontology, Order, OtherSubject, OrderShipped, OrderPacked, OrderReady,
        DeliveryWindow, ShipOrder, DoNothing, AnnounceReady, EmitRichEvent,
    )


def _scenario(domain: EventDomain):
    test = scenario(domain.ontology, store=make_store(domain.ontology))
    test.given(domain.order(id="order-1"))
    return test


def test_then_event_matches_emitted_type_payload_and_about(domain: EventDomain) -> None:
    test = _scenario(domain).when(
        domain.ship_params(order_id="order-1"), by=OPERATOR
    )

    assert test.then_event(
        domain.shipped(carrier="yamato"), about="order-1"
    ) is test
    assert test.then_event(
        domain.shipped(carrier="yamato"), about=domain.order(id="order-1")
    ) is test


def test_then_event_compares_payload_in_the_emission_storage_form(domain: EventDomain) -> None:
    test = _scenario(domain).when(
        domain.rich_params(order_id="order-1"), by=OPERATOR
    )

    assert test.then_event(
        domain.rich(
            day=date(2026, 10, 4),
            dispatched_at=datetime(2026, 10, 4, 9, tzinfo=timezone.utc),
            detail=DeliveryDetail(state=DeliveryState.SHIPPED),
        ),
        about=domain.order(id="order-1"),
    ) is test


def test_then_event_mismatch_lists_each_emitted_event_exactly(domain: EventDomain) -> None:
    test = _scenario(domain).when(
        domain.ship_params(order_id="order-1"), by=OPERATOR
    )

    with pytest.raises(AssertionError) as caught:
        test.then_event(domain.shipped(carrier="other"))

    assert str(caught.value) == (
        "then_event: step 1 ShipOrder by Operator: expected OrderShipped "
        "payload {'carrier': 'other'}; emitted:\n"
        "  OrderShipped about ('Order', 'order-1') payload {'carrier': 'yamato'}\n"
        "  OrderPacked about ('Order', 'order-1') payload {'count': 2}\n"
        "  OrderReady about ('Order', 'order-1') payload {'carrier': 'yamato'}"
    )


def test_then_event_object_about_requires_matching_subject_type(domain: EventDomain) -> None:
    test = _scenario(domain).when(
        domain.ship_params(order_id="order-1"), by=OPERATOR
    )

    with pytest.raises(AssertionError) as caught:
        test.then_event(
            domain.shipped(carrier="yamato"),
            about=domain.other_subject(id="order-1"),
        )

    assert str(caught.value) == (
        "then_event: step 1 ShipOrder by Operator: expected OrderShipped "
        "payload {'carrier': 'yamato'} about ('OtherSubject', 'order-1'); emitted:\n"
        "  OrderShipped about ('Order', 'order-1') payload {'carrier': 'yamato'}\n"
        "  OrderPacked about ('Order', 'order-1') payload {'count': 2}\n"
        "  OrderReady about ('Order', 'order-1') payload {'carrier': 'yamato'}"
    )


def test_then_event_requires_matching_event_type_when_payloads_are_equal(
    domain: EventDomain,
) -> None:
    test = _scenario(domain).when(
        domain.ready_params(order_id="order-1"), by=OPERATOR
    )

    with pytest.raises(AssertionError) as caught:
        test.then_event(domain.shipped(carrier="yamato"))

    assert str(caught.value) == (
        "then_event: step 1 AnnounceReady by Operator: expected OrderShipped "
        "payload {'carrier': 'yamato'}; emitted:\n"
        "  OrderReady about ('Order', 'order-1') payload {'carrier': 'yamato'}"
    )


def test_then_event_object_about_requires_matching_subject_id(domain: EventDomain) -> None:
    test = _scenario(domain).when(
        domain.ship_params(order_id="order-1"), by=OPERATOR
    )

    with pytest.raises(AssertionError) as caught:
        test.then_event(domain.shipped(carrier="yamato"), about="other-order")

    assert str(caught.value) == (
        "then_event: step 1 ShipOrder by Operator: expected OrderShipped "
        "payload {'carrier': 'yamato'} about id 'other-order'; emitted:\n"
        "  OrderShipped about ('Order', 'order-1') payload {'carrier': 'yamato'}\n"
        "  OrderPacked about ('Order', 'order-1') payload {'count': 2}\n"
        "  OrderReady about ('Order', 'order-1') payload {'carrier': 'yamato'}"
    )


def test_then_event_no_events_is_exact_and_ignores_earlier_steps(domain: EventDomain) -> None:
    test = _scenario(domain)
    test.when(domain.ship_params(order_id="order-1"), by=OPERATOR)
    test.then_event(domain.shipped(carrier="yamato"))
    test.when(domain.noop_params(order_id="order-1"), by=OPERATOR)

    with pytest.raises(AssertionError) as caught:
        test.then_event(domain.shipped(carrier="yamato"))

    assert str(caught.value) == (
        "then_event: step 2 DoNothing by Operator: expected OrderShipped "
        "payload {'carrier': 'yamato'}; no events"
    )


def test_then_event_without_a_when_has_exact_message(domain: EventDomain) -> None:
    test = scenario(domain.ontology, store=make_store(domain.ontology))

    with pytest.raises(AssertionError) as caught:
        test.then_event(domain.shipped(carrier="yamato"))

    assert str(caught.value) == "then_event: no successful when to check"


def test_then_event_reports_failed_when_and_chains_original_error(domain: EventDomain) -> None:
    test = _scenario(domain).when(
        domain.ship_params(order_id="order-1"), by=VIEWER
    )

    with pytest.raises(AssertionError) as caught:
        test.then_event(domain.shipped(carrier="yamato"))

    assert str(caught.value) == (
        "then_event: step 1 ShipOrder by Viewer failed with PERMISSION_DENIED"
    )
    assert getattr(caught.value.__cause__, "code", None) == "PERMISSION_DENIED"


def test_then_event_rejects_an_event_from_another_ontology(domain: EventDomain) -> None:
    foreign_ontology = Ontology("foreign-events", scope_levels=["org"], min_n=1)

    @foreign_ontology.event()
    class ForeignEvent(Event):
        value: str

    test = _scenario(domain).when(
        domain.noop_params(order_id="order-1"), by=OPERATOR
    )

    with pytest.raises(ValidationFailed) as caught:
        test.then_event(ForeignEvent(value="x"))

    assert caught.value.code == "UNKNOWN_NAME"
    assert str(caught.value) == (
        "'ForeignEvent' (api_name 'ForeignEvent') was registered on a different "
        "Ontology -- it is not part of this scenario's ontology"
    )


def test_then_event_rejects_an_unregistered_event(domain: EventDomain) -> None:
    class UnregisteredEvent(Event):
        value: str

    test = _scenario(domain).when(
        domain.noop_params(order_id="order-1"), by=OPERATOR
    )

    with pytest.raises(ValidationFailed) as caught:
        test.then_event(UnregisteredEvent(value="x"))

    assert caught.value.code == "UNKNOWN_NAME"
    assert str(caught.value) == (
        "'UnregisteredEvent' is not decorated with @ontology.event(...) "
        "-- it has no registered api_name"
    )
