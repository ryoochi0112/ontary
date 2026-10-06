"""Business facts and the six permitted maintenance verbs.

Money is recorded in integer minor units, in a single deployment currency.
Catalogue data is source-backed; operational facts are exclusively action-owned.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Literal

from pydantic import BaseModel

from ontary import (
    ActionContext,
    ActionError,
    ActionParams,
    BoundQuery,
    Cardinality,
    Event,
    FunctionParams,
    ObjectStore,
    Ontology,
    OntologyObject,
    SelfScope,
    Sensitivity,
    ViaLink,
    prop,
    ref,
    scope_ref,
    target,
)
from ontary.meta import TransitionDef

Severity = Literal["minor", "major", "critical"]
Priority = Literal["low", "normal", "high", "urgent"]
Status = Literal["reported", "triaged", "scheduled", "done", "cancelled"]
DEADLINES = {
    "urgent": timedelta(hours=4),
    "high": timedelta(hours=24),
    "normal": timedelta(days=3),
    "low": timedelta(days=7),
}
MOVES = {
    "reported": ("triaged", "cancelled"),
    "triaged": ("scheduled", "cancelled"),
    "scheduled": ("done", "cancelled"),
    "done": (),
    "cancelled": (),
}
OPEN_STATUSES = ("reported", "triaged", "scheduled")
ROLES = ["Technician", "Manager"]
# min_n gates engine aggregates; 3 (the default) with Sensitivity declared triggers diagnose's
# MIN_N_UNSET, 1 disables the guard; 2 still refuses a one-person aggregate and every per-site
# fixture population clears it.
ontology = Ontology("maintenance_desk", scope_levels=["site", "region"], min_n=2)


def via(name: str, parent: str) -> list[ViaLink]:
    return [ViaLink(link_api_name=name, direction="from", parent_type=parent)]


@ontology.object(layer="L0", scope=[SelfScope(level="region")])
class Region(OntologyObject):
    id: str = prop(primary_key=True)
    name: str


@ontology.object(layer="L0", scope=[SelfScope(level="site"), *via("site_region", "Region")])
class Site(OntologyObject):
    id: str = prop(primary_key=True)
    name: str


class Location(BaseModel):
    building: str
    floor: str


@ontology.object(layer="L0", scope=via("asset_site", "Site"))
class Asset(OntologyObject):
    id: str = prop(primary_key=True)
    name: str
    location: Location


@ontology.object(layer="L0", scope=via("technician_site", "Site"))
class Technician(OntologyObject):
    id: str = prop(primary_key=True)
    name: str
    phone: str | None = prop(default=None, sensitivity=Sensitivity(ai_usable=False))
    email: str | None = prop(default=None, sensitivity=Sensitivity(ai_usable=False))


@ontology.object(layer="L0", scope="unscoped")
class Part(OntologyObject):
    id: str = prop(primary_key=True)
    part_number: str
    name: str
    unit_cost: int


@ontology.object(layer="L0", owned=True, scope=via("order_asset", "Asset"))
class WorkOrder(OntologyObject):
    id: str = prop(primary_key=True)
    fault: str
    severity: Severity
    priority: Priority
    status: Status = prop(transitions=TransitionDef(initial=("reported",), moves=MOVES))
    reported_at: datetime
    response_deadline: datetime


@ontology.object(layer="L0", owned=True, scope=via("usage_order", "WorkOrder"), snapshot=True)
class PartsUsage(OntologyObject):
    id: str = prop(primary_key=True)
    quantity: int
    unit_cost: int
    used_at: datetime


@ontology.rule(PartsUsage, "valid_usage", message="quantity must be positive and cost nonnegative")
def valid_usage(usage: PartsUsage) -> bool:
    return usage.quantity > 0 and usage.unit_cost >= 0


@ontology.object(layer="L0", owned=True, scope=via("reservation_order", "WorkOrder"))
class Reservation(OntologyObject):
    id: str = prop(primary_key=True)
    quantity: int


@ontology.rule(Reservation, "positive_quantity", message="reserved quantity must be positive")
def positive_quantity(reservation: Reservation) -> bool:
    return reservation.quantity > 0


site_region = ontology.link("site_region", Site, Region, Cardinality.MANY_TO_ONE)
asset_site = ontology.link("asset_site", Asset, Site, Cardinality.MANY_TO_ONE)
technician_site = ontology.link("technician_site", Technician, Site, Cardinality.MANY_TO_ONE)
order_asset = ontology.link("order_asset", WorkOrder, Asset, Cardinality.MANY_TO_ONE, owned=True)
usage_order = ontology.link(
    "usage_order", PartsUsage, WorkOrder, Cardinality.MANY_TO_ONE, owned=True
)
usage_part = ontology.link("usage_part", PartsUsage, Part, Cardinality.MANY_TO_ONE, owned=True)
reservation_order = ontology.link(
    "reservation_order", Reservation, WorkOrder, Cardinality.MANY_TO_ONE, owned=True
)
reservation_part = ontology.link(
    "reservation_part", Reservation, Part, Cardinality.MANY_TO_ONE, owned=True
)
order_technician = ontology.link(
    "order_technician", WorkOrder, Technician, Cardinality.MANY_TO_ONE, owned=True
)


@ontology.event(description="Maintenance completed; ready for invoicing and asset history.")
class WorkOrderCompleted(Event):
    asset_id: str


class ReportFault(ActionParams):
    asset_id: str = scope_ref(Asset)
    fault: str
    severity: Severity
    priority: Priority


class OrderAction(ActionParams):
    order_id: str = target(WorkOrder)


class Triage(OrderAction):
    pass


class Schedule(OrderAction):
    technician_id: str = target(Technician)
    reserve_part_id: str | None = ref(Part, default=None)
    reserve_quantity: int | None = None


class Complete(OrderAction):
    pass


class Cancel(OrderAction):
    pass


class RecordPartsUsed(OrderAction):
    part_id: str = ref(Part)
    quantity: int


def get_order(ctx: ActionContext, order_id: str) -> WorkOrder:
    order = ctx.get(WorkOrder, order_id)
    if order is None:
        raise ActionError("work order does not exist", code="PRECONDITION_FAILED")
    return order


@ontology.action(ReportFault, api_name="report_fault", target=WorkOrder, roles=ROLES)
def report_fault(ctx: ActionContext, p: ReportFault) -> dict[str, str]:
    asset = ctx.get(Asset, p.asset_id)
    if asset is None:
        raise ActionError("asset does not exist", code="PRECONDITION_FAILED")
    order = ctx.create(
        WorkOrder,
        fault=p.fault,
        severity=p.severity,
        priority=p.priority,
        status="reported",
        reported_at=ctx.now(),
        response_deadline=ctx.now() + DEADLINES[p.priority],
    )
    ctx.link(order_asset, order, asset)
    return {"order_id": order.id}


@ontology.action(Triage, api_name="triage", target=WorkOrder, roles=ROLES)
def triage(ctx: ActionContext, p: Triage) -> dict[str, str]:
    order = get_order(ctx, p.order_id)
    order.status = "triaged"
    ctx.save(order)
    return {"status": order.status}


@ontology.action(Schedule, api_name="schedule", target=WorkOrder, roles=ROLES)
def schedule(ctx: ActionContext, p: Schedule) -> dict[str, str]:
    order = get_order(ctx, p.order_id)
    technician = ctx.get(Technician, p.technician_id)
    if technician is None:
        raise ActionError("technician does not exist", code="PRECONDITION_FAILED")
    asset = ctx.traverse(order_asset, order)[0]
    if ctx.traverse(asset_site, asset)[0].id != ctx.traverse(technician_site, technician)[0].id:
        raise ActionError("technician must work at the asset's site", code="PRECONDITION_FAILED")
    order.status = "scheduled"
    ctx.save(order)
    ctx.link(order_technician, order, technician)
    if p.reserve_part_id is not None:
        if ctx.get(Part, p.reserve_part_id) is None:
            raise ActionError("part does not exist", code="PRECONDITION_FAILED")
        reservation = ctx.create(Reservation, quantity=p.reserve_quantity or 0)
        ctx.link(reservation_order, reservation, order)
        ctx.link(reservation_part, reservation, p.reserve_part_id)
    elif p.reserve_quantity is not None:
        raise ActionError("reservation quantity requires a part", code="PRECONDITION_FAILED")
    return {"status": order.status}


@ontology.action(
    Complete, api_name="complete", target=WorkOrder, roles=ROLES, emits=[WorkOrderCompleted]
)
def complete(ctx: ActionContext, p: Complete) -> dict[str, str]:
    order = get_order(ctx, p.order_id)
    order.status = "done"
    ctx.save(order)
    ctx.emit(WorkOrderCompleted(asset_id=ctx.traverse(order_asset, order)[0].id))
    return {"status": order.status}


@ontology.action(Cancel, api_name="cancel", target=WorkOrder, roles=ROLES)
def cancel(ctx: ActionContext, p: Cancel) -> dict[str, str]:
    order = get_order(ctx, p.order_id)
    order.status = "cancelled"
    ctx.save(order)
    for reservation in ctx.traverse(reservation_order, order, reverse=True):
        ctx.retire(reservation)
    return {"status": order.status}


@ontology.action(RecordPartsUsed, api_name="record_parts_used", target=WorkOrder, roles=ROLES)
def record_parts_used(ctx: ActionContext, p: RecordPartsUsed) -> dict[str, str]:
    order = get_order(ctx, p.order_id)
    part = ctx.get(Part, p.part_id)
    if order.status != "scheduled" or part is None:
        raise ActionError(
            "parts usage needs a scheduled order and an existing part",
            code="PRECONDITION_FAILED",
        )
    usage = ctx.create(PartsUsage, quantity=p.quantity, unit_cost=part.unit_cost, used_at=ctx.now())
    ctx.link(usage_order, usage, order)
    ctx.link(usage_part, usage, part)
    remaining = p.quantity
    for reservation in ctx.traverse(reservation_order, order, reverse=True):
        if ctx.traverse(reservation_part, reservation)[0].id == part.id and remaining:
            consumed = min(reservation.quantity, remaining)
            remaining -= consumed
            reservation.quantity -= consumed
            if reservation.quantity:
                ctx.save(reservation)
            else:
                ctx.retire(reservation)
    return {"usage_id": usage.id}


class SiteQuestion(FunctionParams):
    site_id: str = ref(Site)


@ontology.function(
    SiteQuestion,
    description="Open work orders past their response deadline at a site.",
)
def overdue(query: BoundQuery, p: SiteQuestion) -> list[dict[str, object]]:
    query.get(Site, p.site_id)  # Explicit site requests must pass the same guarded read gate.
    open_late = query.list(
        WorkOrder,
        where={
            "status": {"in": list(OPEN_STATUSES)},
            "response_deadline": {"lt": query.now()},
        },
        limit=None,
    )
    return [
        order.model_dump(mode="json")
        for order in open_late
        if query.traverse(asset_site, query.traverse(order_asset, order.id)[0].id)[0].id
        == p.site_id
    ]


class MaintenanceCost(SiteQuestion):
    pass


@ontology.function(MaintenanceCost, description="Parts maintenance cost per asset, in minor units.")
def maintenance_cost(query: BoundQuery, p: MaintenanceCost) -> dict[str, int]:
    query.get(Site, p.site_id)
    costs: dict[str, int] = {}
    for asset in query.list(Asset):
        if query.traverse(asset_site, asset.id)[0].id != p.site_id:
            continue
        costs[asset.id] = sum(
            usage.quantity * usage.unit_cost
            for order in query.traverse(order_asset, asset.id, reverse=True)
            for usage in query.traverse(usage_order, order.id, reverse=True)
        )
    return costs


ontology.validate()


def build_ontology(*, clock: Callable[[], datetime] | None = None) -> tuple[Ontology, ObjectStore]:
    """Return the validated ontology paired with a fresh store.

    A store has one clock: pass `clock` to install it, then bind runtimes with the same one.
    """
    store = ObjectStore(ontology.registry)
    if clock is not None:
        store.bind_clock(clock)
    return ontology, store
