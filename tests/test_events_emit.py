"""Action emission (#47): ordered facts, subject guards, and atomic rollback."""

from __future__ import annotations

import subprocess
import sys
import textwrap
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel

from ontary import ActionContext, ActionParams, Ontology, OntologyObject, Source, prop, ref, target
from ontary.audit import EmittedEvent
from ontary.errors import InternalError, PermissionDenied, ValidationFailed
from ontary.meta import Sensitivity
from ontary.model import Event
from ontary.store.inmemory import InMemoryStore
from ontary.testing import FixedClock, SequentialIds, consumer

INSTANT = datetime(2026, 10, 4, 9, tzinfo=timezone.utc)
PAYLOAD_INSTANT = datetime(2026, 10, 4, 18, 30, tzinfo=timezone(timedelta(hours=9)))
Handler = Callable[[ActionContext], dict[str, Any]]


class ShippingState(Enum):
    SHIPPED = "shipped"


class ShipmentDetail(BaseModel):
    label: str
    count: int
    state: ShippingState
    dispatched_at: datetime
    dispatch_day: date
    note: str | None = None


@dataclass
class EmitDomain:
    ontology: Ontology
    order: type[OntologyObject]
    customer: type[OntologyObject]
    shipped: type[Event]
    packed: type[Event]
    store: InMemoryStore
    clock: FixedClock

    def execute(
        self,
        handler: Handler,
        *,
        emits: list[type[Event]] | None = None,
        target_id: str | None = "order-1",
        reference_only: bool = False,
        role: str = "Operator",
    ) -> dict[str, Any]:
        if target_id is None:
            class ShipParams(ActionParams):
                pass

            params = ShipParams()
        else:
            marker = ref if reference_only else target

            class ShipParams(ActionParams):
                order_id: str = marker(self.order)

            params = ShipParams(order_id=target_id)

        @self.ontology.action(
            ShipParams, target=self.order, roles=["Operator"], api_name="ShipOrder",
            emits=[self.shipped] if emits is None else emits,
        )
        def ship(ctx: ActionContext, params: ShipParams) -> dict[str, Any]:
            return handler(ctx)

        client = self.ontology.bind(
            self.store, clock=self.clock, id_factory=SequentialIds("inv")
        ).for_consumer(consumer(role=role, kind="ai"))
        return client.execute(params)


@pytest.fixture
def domain() -> EmitDomain:
    ontology = Ontology("event-emission", scope_levels=["org"], min_n=1)

    @ontology.object(layer="L0", scope="unscoped", owned=True)
    class Order(OntologyObject):
        id: str = prop(primary_key=True)
        status: str

    @ontology.object(layer="L0", scope="unscoped", owned=True)
    class Customer(OntologyObject):
        id: str = prop(primary_key=True)
        name: str

    @ontology.event()
    class OrderShipped(Event):
        carrier: str
        at: datetime | None = None
        day: date | None = None
        state: ShippingState | None = None
        detail: ShipmentDetail | None = None
        secret: str | None = prop(sensitivity=Sensitivity(ai_usable=False))

    @ontology.event(api_name="Packed")
    class OrderPacked(Event):
        carrier: str

    store = InMemoryStore(ontology.registry)
    clock = FixedClock(INSTANT)
    store.bind_clock(clock)
    source = Source(source_system="seed")
    store.insert("Order", {"id": "order-1", "status": "new"}, source)
    store.insert("Order", {"id": "order-2", "status": "new"}, source)
    store.insert("Customer", {"id": "order-1", "name": "Ada"}, source)
    return EmitDomain(ontology, Order, Customer, OrderShipped, OrderPacked, store, clock)


def _fact(domain: EmitDomain, carrier: str = "yamato") -> Event:
    return domain.shipped(carrier=carrier)


def _assert_refusal(
    domain: EmitDomain,
    handler: Handler,
    *,
    code: str,
    message: str,
    emits: list[type[Event]] | None = None,
    target_id: str | None = "order-1",
) -> None:
    before_orders = domain.store.read_all("Order")
    before_customers = domain.store.read_all("Customer")

    def attempt(ctx: ActionContext) -> dict[str, Any]:
        created = ctx.create(domain.order, id="rollback-order", status="pending")
        order = ctx.get(domain.order, "order-1")
        assert order is not None
        order.status = "changed"
        ctx.save(order)
        if emits is None or domain.shipped in emits:
            ctx.emit(_fact(domain, "before-refusal"), about=created)
        return handler(ctx)

    with pytest.raises(ValidationFailed) as caught:
        domain.execute(attempt, emits=emits, target_id=target_id)
    assert caught.value.code == code
    assert str(caught.value) == message
    assert domain.store.read_all("Order") == before_orders
    assert domain.store.read_all("Customer") == before_customers
    assert domain.store.read_last("Order", "rollback-order") is None
    assert len(domain.store._objects) == 3
    entries = domain.store.audit_entries()
    assert len(entries) == 1
    entry = entries[0]
    assert entry.kind == "action"
    assert entry.outcome == "error"
    assert entry.error_code == code
    assert entry.writes == []
    assert entry.events == []
    assert entry.ts == INSTANT
    assert entry.invocation_id == "inv-1"


@pytest.mark.parametrize("reference_only", [False, True])
def test_emit_uses_resolved_target_and_property_storage_form(
    domain: EmitDomain, reference_only: bool,
) -> None:
    observed: list[datetime] = []

    def ship(ctx: ActionContext) -> dict[str, Any]:
        observed.append(ctx.now())
        order = ctx.get(domain.order, "order-1")
        assert order is not None
        order.status = "shipped"
        ctx.save(order)
        ctx.emit(domain.shipped(
            carrier="yamato", at=PAYLOAD_INSTANT, day=date(2026, 10, 4),
            state=ShippingState.SHIPPED,
            detail=ShipmentDetail(
                label="box", count=2, state=ShippingState.SHIPPED,
                dispatched_at=PAYLOAD_INSTANT, dispatch_day=date(2026, 10, 4),
            ),
            secret="administrative payload",
        ))
        return {"shipped": True}

    assert domain.execute(ship, reference_only=reference_only) == {"shipped": True}
    entries = domain.store.audit_entries()
    assert len(entries) == 1
    entry = entries[0]
    assert entry.kind == "action"
    assert entry.outcome == "ok"
    assert entry.error_code is None
    assert entry.ts == observed[0] == domain.clock() == INSTANT
    assert entry.invocation_id == "inv-1"
    assert entry.target_type == "Order"
    assert entry.target_id == "order-1"
    assert entry.events == [EmittedEvent(
        event_type="OrderShipped", about_type="Order", about_id="order-1",
        payload={
            "carrier": "yamato", "at": PAYLOAD_INSTANT.isoformat(), "day": "2026-10-04",
            "state": "shipped", "detail": {
                "label": "box", "count": 2, "state": "shipped",
                "dispatched_at": PAYLOAD_INSTANT.isoformat(), "dispatch_day": "2026-10-04",
                "note": None,
            },
            "secret": "administrative payload",
        },
    )]
    assert type(entry.events[0].payload["state"]) is str
    assert type(entry.events[0].payload["detail"]["state"]) is str
    row = domain.store.read_current("Order", "order-1")
    assert row is not None
    assert row.payload["status"] == "shipped"


def test_multiple_emits_preserve_order_and_event_api_names(domain: EmitDomain) -> None:
    def ship(ctx: ActionContext) -> dict[str, Any]:
        ctx.emit(_fact(domain, "first"))
        ctx.emit(domain.packed(carrier="second"))
        ctx.emit(_fact(domain, "third"))
        return {}

    domain.execute(ship, emits=[domain.packed, domain.shipped])
    events = domain.store.audit_entries()[0].events
    assert [(event.event_type, event.payload["carrier"]) for event in events] == [
        ("OrderShipped", "first"), ("Packed", "second"), ("OrderShipped", "third"),
    ]
    assert [(event.about_type, event.about_id) for event in events] == [("Order", "order-1")] * 3


def test_creating_action_emits_about_created_object_without_target_id(domain: EmitDomain) -> None:
    def create(ctx: ActionContext) -> dict[str, Any]:
        order = ctx.create(domain.order, status="new")
        ctx.emit(_fact(domain), about=order)
        return {"order_id": order.id}

    assert domain.execute(create, target_id=None) == {"order_id": "inv-2"}
    entry = domain.store.audit_entries()[0]
    assert entry.outcome == "ok"
    assert entry.target_id is None
    assert entry.invocation_id == "inv-1"
    assert entry.ts == INSTANT
    assert [(event.about_type, event.about_id) for event in entry.events] == [("Order", "inv-2")]
    assert domain.store.read_current("Order", "inv-2") is not None


@pytest.mark.parametrize("read", ["get", "all"])
def test_explicit_loaded_subject_overrides_resolved_target(domain: EmitDomain, read: str) -> None:
    def ship(ctx: ActionContext) -> dict[str, Any]:
        order = (
            ctx.get(domain.order, "order-2") if read == "get"
            else next(obj for obj in ctx.all(domain.order) if obj.id == "order-2")
        )
        assert order is not None
        ctx.emit(_fact(domain), about=order)
        return {}

    domain.execute(ship)
    entry = domain.store.audit_entries()[0]
    assert entry.target_id == "order-1"
    assert entry.events[0].about_id == "order-2"


@pytest.mark.parametrize("subclass", [False, True])
def test_unstamped_event_is_refused_before_subject_resolution(
    domain: EmitDomain, subclass: bool,
) -> None:
    base = domain.shipped if subclass else Event

    class Unregistered(base):
        carrier: str

    def ship(ctx: ActionContext) -> dict[str, Any]:
        ctx.emit(Unregistered(carrier="yamato"))
        return {}

    _assert_refusal(
        domain, ship, code="UNKNOWN_NAME", target_id=None,
        message="'Unregistered' is not decorated with @ontology.event(...) "
        "-- it has no registered api_name",
    )


def test_foreign_event_with_same_api_name_is_refused(domain: EmitDomain) -> None:
    foreign = Ontology("foreign", scope_levels=["org"])

    @foreign.event()
    class OrderShipped(Event):
        carrier: str

    def ship(ctx: ActionContext) -> dict[str, Any]:
        ctx.emit(OrderShipped(carrier="yamato"))
        return {}

    _assert_refusal(
        domain, ship, code="UNKNOWN_NAME",
        message="'OrderShipped' (api_name 'OrderShipped') was registered on a different "
        "Ontology -- it is not part of this action's ontology",
    )


def test_stamped_event_name_must_have_a_registry_descriptor(domain: EmitDomain) -> None:
    class MissingEvent(Event):
        carrier: str

    MissingEvent._ontary_api_name = "MissingEvent"
    MissingEvent._ontary_registry = domain.ontology.registry

    def ship(ctx: ActionContext) -> dict[str, Any]:
        ctx.emit(MissingEvent(carrier="yamato"))
        return {}

    _assert_refusal(
        domain, ship, code="UNKNOWN_NAME",
        message="unregistered event type: 'MissingEvent'",
    )


@pytest.mark.parametrize("emits_other", [False, True])
def test_event_not_in_emits_is_refused_before_subject_resolution(
    domain: EmitDomain, emits_other: bool,
) -> None:
    def ship(ctx: ActionContext) -> dict[str, Any]:
        ctx.emit(_fact(domain))
        return {}

    _assert_refusal(
        domain, ship, code="UNDECLARED_EVENT", target_id=None,
        emits=[domain.packed] if emits_other else [],
        message="ActionContext.emit: action 'ShipOrder' did not declare event 'OrderShipped' in emits",
    )


def test_no_resolved_target_requires_explicit_subject(domain: EmitDomain) -> None:
    def ship(ctx: ActionContext) -> dict[str, Any]:
        ctx.emit(_fact(domain))
        return {}

    _assert_refusal(
        domain, ship, code="EVENT_SUBJECT_INVALID", target_id=None,
        message="ActionContext.emit: action 'ShipOrder' has no resolved target id "
        "-- pass about= an object from this action context",
    )


@pytest.mark.parametrize("origin", ["handbuilt", "other_context", "forged_identity"])
def test_explicit_subject_must_be_handed_out_by_this_context(
    domain: EmitDomain, origin: str,
) -> None:
    if origin == "other_context":
        other = ActionContext(
            domain.store, Source(source_system="other"), consumer(), registry=domain.ontology.registry
        )
        subject = other.get(domain.order, "order-1")
    else:
        subject = domain.order(id="order-1", status="new")
    assert subject is not None

    def ship(ctx: ActionContext) -> dict[str, Any]:
        if origin == "forged_identity":
            loaded = ctx.get(domain.order, "order-1")
            assert loaded is not None
            ctx._loaded[id(subject)] = ctx._loaded[id(loaded)]
        ctx.emit(_fact(domain), about=subject)
        return {}

    _assert_refusal(
        domain, ship, code="EVENT_SUBJECT_INVALID",
        message="ActionContext.emit: this Order was not loaded by this action context "
        "-- get it with ctx.get(...) or ctx.create(...) first",
    )


def test_loaded_subject_must_have_actions_target_type(domain: EmitDomain) -> None:
    def ship(ctx: ActionContext) -> dict[str, Any]:
        customer = ctx.get(domain.customer, "order-1")
        assert customer is not None
        ctx.emit(_fact(domain), about=customer)
        return {}

    _assert_refusal(
        domain, ship, code="EVENT_SUBJECT_INVALID",
        message="ActionContext.emit: subject type 'Customer' does not match "
        "action 'ShipOrder' target type 'Order'",
    )


def test_default_subject_must_exist_at_emit_time(domain: EmitDomain) -> None:
    def ship(ctx: ActionContext) -> dict[str, Any]:
        ctx.emit(_fact(domain))
        ctx.create(domain.order, id="not-created", status="new")
        return {}

    _assert_refusal(
        domain, ship, code="EVENT_SUBJECT_INVALID", target_id="not-created",
        message="ActionContext.emit: subject Order 'not-created' has no stored row at emit time",
    )
    assert domain.store.read_last("Order", "not-created") is None


def test_explicit_subject_must_exist_at_emit_time(domain: EmitDomain) -> None:
    def ship(ctx: ActionContext) -> dict[str, Any]:
        subject = ctx.get(domain.order, "order-1")
        assert subject is not None
        subject.id = "not-created"
        ctx.emit(_fact(domain), about=subject)
        return {}

    _assert_refusal(
        domain, ship, code="EVENT_SUBJECT_INVALID",
        message="ActionContext.emit: subject Order 'not-created' has no stored row at emit time",
    )


def test_handler_exception_discards_emits_and_all_writes(domain: EmitDomain) -> None:
    before = domain.store.read_all("Order")

    def ship(ctx: ActionContext) -> dict[str, Any]:
        order = ctx.get(domain.order, "order-1")
        assert order is not None
        order.status = "shipped"
        ctx.save(order)
        ctx.create(domain.order, id="rollback-order", status="new")
        ctx.emit(_fact(domain))
        raise RuntimeError("handler failed after emission")

    with pytest.raises(RuntimeError) as caught:
        domain.execute(ship)
    assert str(caught.value) == "handler failed after emission"
    assert domain.store.read_all("Order") == before
    assert domain.store.read_last("Order", "rollback-order") is None
    assert len(domain.store._objects) == 3
    entries = domain.store.audit_entries()
    assert len(entries) == 1
    assert entries[0].outcome == "error"
    assert entries[0].error_code == "INTERNAL_ERROR"
    assert entries[0].events == []
    assert entries[0].writes == []


def test_emit_then_retire_subject_commits_event(domain: EmitDomain) -> None:
    def retire(ctx: ActionContext) -> dict[str, Any]:
        subject = ctx.get(domain.order, "order-1")
        assert subject is not None
        ctx.emit(_fact(domain), about=subject)
        ctx.retire(subject)
        return {}

    domain.execute(retire)
    assert domain.store.read_current("Order", "order-1") is None
    assert domain.store.read_last("Order", "order-1") is not None
    entry = domain.store.audit_entries()[0]
    assert entry.outcome == "ok"
    assert entry.events[0].about_id == "order-1"
    assert entry.writes[0].op == "retire"


def test_default_subject_can_resolve_to_a_retired_row(domain: EmitDomain) -> None:
    domain.store.retire_object("Order", "order-1")

    def ship(ctx: ActionContext) -> dict[str, Any]:
        ctx.emit(_fact(domain))
        return {}

    domain.execute(ship)
    entry = domain.store.audit_entries()[0]
    assert entry.outcome == "ok"
    assert entry.events[0].about_id == "order-1"


def test_denied_invocation_has_no_events(domain: EmitDomain) -> None:
    before = domain.store.read_all("Order")

    def ship(ctx: ActionContext) -> dict[str, Any]:
        pytest.fail("a denied invocation must not reach the handler")

    with pytest.raises(PermissionDenied) as caught:
        domain.execute(ship, role="Reader")
    assert caught.value.code == "PERMISSION_DENIED"
    assert str(caught.value) == "role 'Reader' may not execute 'ShipOrder'"
    assert domain.store.read_all("Order") == before
    entry = domain.store.audit_entries()[0]
    assert entry.outcome == "denied"
    assert entry.error_code == "PERMISSION_DENIED"
    assert entry.events == []
    assert entry.writes == []


def test_direct_context_requires_executor_action_definition(domain: EmitDomain) -> None:
    ctx = ActionContext(
        domain.store, Source(source_system="direct"), consumer(), registry=domain.ontology.registry
    )
    with pytest.raises(InternalError) as caught:
        ctx.emit(_fact(domain))
    assert caught.value.code == "INTERNAL_ERROR"
    assert str(caught.value) == "ActionContext.emit requires the executor's action definition"


def test_mypy_accepts_event_and_refuses_object(tmp_path: Path) -> None:
    snippet = tmp_path / "emit_typing.py"
    snippet.write_text(textwrap.dedent("""\
        from ontary.actions import ActionContext
        from ontary.model import Event, OntologyObject

        class OrderShipped(Event):
            carrier: str

        def handler(ctx: ActionContext, order: OntologyObject) -> None:
            ctx.emit(OrderShipped(carrier="yamato"))
            ctx.emit(OrderShipped(carrier="yamato"), about=order)
            ctx.emit(object())
    """))
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [sys.executable, "-m", "mypy", "--strict", "--no-incremental",
         "--config-file", str(root / "pyproject.toml"), str(snippet)],
        capture_output=True, text=True, cwd=root,
    )
    assert result.returncode == 1, result.stdout + result.stderr
    assert ': error: Argument 1 to "emit" of "ActionContext" has incompatible type "object"; ' \
        'expected "Event"  [arg-type]' in result.stdout
    assert result.stdout.count(": error:") == 1, result.stdout
