"""Business guarantees of the maintenance_desk example, as given/when/then tests.

Every test drives the example through `OntologyClient`, the way application code would. The twelve
numbered guarantees come from the example's brief; each test names the situation, the event and
the outcome. One world is seeded per test: reference data from `load_fixtures`, work orders from
the six actions at `FIXTURE_NOW`, on a clock the test can move.
"""

from __future__ import annotations

import inspect
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from examples.maintenance_desk.fixtures import (
    FIXTURE_NOW,
    FixtureClock,
    assistant_consumer,
    load_fixtures,
    manager_consumer,
    seed_work_orders,
    technician_consumer,
)
from examples.maintenance_desk.ontology import (
    DEADLINES,
    MOVES,
    OPEN_STATUSES,
    Asset,
    Cancel,
    Complete,
    Location,
    MaintenanceCost,
    Part,
    PartsUsage,
    RecordPartsUsed,
    ReportFault,
    Reservation,
    Schedule,
    SiteQuestion,
    Technician,
    Triage,
    WorkOrder,
    WorkOrderCompleted,
    build_ontology,
    ontology,
    order_asset,
    order_technician,
    reservation_order,
    usage_order,
)
from ontary import Consumer, Ontology, OntologyClient, Source
from ontary.store import Store
from ontary.testing import SequentialIds, raises_code

_SRC = Source(source_system="synthetic")


@dataclass
class World:
    """A seeded store, its movable clock, and the ids and orders the fixtures created."""

    onto: Ontology
    store: Store
    clock: FixtureClock
    ids: dict[str, str]
    orders: dict[str, str]
    manager: OntologyClient

    def client(self, who: Consumer) -> OntologyClient:
        return self.onto.bind(self.store, clock=self.clock).for_consumer(who)

    def technician(self, site: str = "A") -> OntologyClient:
        return self.client(technician_consumer(self.ids, site))

    def assistant(self, site: str = "A") -> OntologyClient:
        return self.client(assistant_consumer(self.ids, site))


def make_world() -> World:
    clock = FixtureClock(FIXTURE_NOW)
    onto, store = build_ontology(clock=clock)
    ids = load_fixtures(store)
    manager = onto.bind(store, clock=clock, id_factory=SequentialIds("wo")).for_consumer(
        manager_consumer(ids)
    )
    return World(onto, store, clock, ids, seed_work_orders(manager, ids), manager)


@pytest.fixture
def world() -> World:
    return make_world()


def report(world: World, asset: str = "asset_a0_id", priority: str = "normal") -> str:
    order = world.manager.execute(
        "report_fault",
        {
            "asset_id": world.ids[asset],
            "fault": "Seal leaks",
            "severity": "major",
            "priority": priority,
        },
    )
    return str(order["order_id"])


def order_in(world: World, status: str) -> str:
    """A fresh site-A order that has reached `status` through the actions."""
    order_id = report(world)
    if status == "reported":
        return order_id
    if status == "cancelled":
        world.manager.execute(Cancel(order_id=order_id))
        return order_id
    world.manager.execute(Triage(order_id=order_id))
    if status in ("scheduled", "done"):
        world.manager.execute(Schedule(order_id=order_id, technician_id=world.ids["tech_a0_id"]))
    if status == "done":
        world.manager.execute(Complete(order_id=order_id))
    return order_id


def move_to(world: World, order_id: str, status: str) -> None:
    {
        "triaged": lambda: world.manager.execute(Triage(order_id=order_id)),
        "scheduled": lambda: world.manager.execute(
            Schedule(order_id=order_id, technician_id=world.ids["tech_a1_id"])
        ),
        "done": lambda: world.manager.execute(Complete(order_id=order_id)),
        "cancelled": lambda: world.manager.execute(Cancel(order_id=order_id)),
    }[status]()


# -- the clock the fixtures use ---------------------------------------------------------------


def test_given_a_fixture_clock_when_set_then_it_reads_the_new_instant() -> None:
    clock = FixtureClock(FIXTURE_NOW)

    clock.set(FIXTURE_NOW + timedelta(days=2))

    assert clock() == FIXTURE_NOW + timedelta(days=2)


def test_given_a_naive_datetime_when_building_or_setting_a_fixture_clock_then_it_is_refused() -> (
    None
):
    naive = datetime(2026, 10, 5, 9)
    clock = FixtureClock(FIXTURE_NOW)

    with pytest.raises(ValueError, match="timezone-aware"):
        FixtureClock(naive)
    with pytest.raises(ValueError, match="timezone-aware"):
        clock.set(naive)
    assert clock() == FIXTURE_NOW


def test_given_a_moved_clock_when_a_fault_is_reported_then_the_order_is_stamped_at_the_new_time(
    world: World,
) -> None:
    later = FIXTURE_NOW + timedelta(days=1)
    world.clock.set(later)

    order_id = report(world, priority="urgent")

    order = world.manager.get(WorkOrder, order_id)
    assert order.reported_at == later


# -- guarantee 1: severity and priority are closed sets ---------------------------------------


@pytest.mark.parametrize(
    ("field", "bad"), [("severity", "catastrophic"), ("priority", "whenever")]
)
def test_given_a_value_outside_the_allowed_set_when_a_fault_is_reported_then_it_is_refused(
    world: World, field: str, bad: str
) -> None:
    params = {
        "asset_id": world.ids["asset_a0_id"],
        "fault": "Seal leaks",
        "severity": "minor",
        "priority": "low",
        field: bad,
    }
    before = len(world.manager.list(WorkOrder, limit=None))

    with raises_code("INVALID_PARAMS"):
        world.manager.execute("report_fault", params)
    with pytest.raises(ValidationError):
        ReportFault.model_validate(params)

    assert len(world.manager.list(WorkOrder, limit=None)) == before


@pytest.mark.parametrize("severity", ["minor", "major", "critical"])
@pytest.mark.parametrize("priority", ["low", "normal", "high", "urgent"])
def test_given_every_allowed_severity_and_priority_when_a_fault_is_reported_then_it_is_accepted(
    world: World, severity: str, priority: str
) -> None:
    order = world.manager.execute(
        "report_fault",
        {
            "asset_id": world.ids["asset_a0_id"],
            "fault": "Seal leaks",
            "severity": severity,
            "priority": priority,
        },
    )

    stored = world.manager.get(WorkOrder, str(order["order_id"]))
    assert (stored.severity, stored.priority, stored.status) == (severity, priority, "reported")


# -- guarantee 2: the move graph ---------------------------------------------------------------
# A move to the status an order already holds is a no-op, not a transition: not listed.

_STATUSES = list(MOVES)
_ACTION_TARGETS = ["triaged", "scheduled", "done", "cancelled"]  # "reported" has no action
_LEGAL = [(src, dst) for src in _STATUSES for dst in _ACTION_TARGETS if dst in MOVES[src]]
_ILLEGAL = [(src, dst) for src in _STATUSES for dst in _ACTION_TARGETS if dst not in MOVES[src] and dst != src]


@pytest.mark.parametrize(("src", "dst"), _LEGAL, ids=[f"{s}-to-{d}" for s, d in _LEGAL])
def test_given_an_order_when_it_takes_a_legal_move_then_it_reaches_the_new_status(
    world: World, src: str, dst: str
) -> None:
    order_id = order_in(world, src)

    move_to(world, order_id, dst)

    assert world.manager.get(WorkOrder, order_id).status == dst


@pytest.mark.parametrize(("src", "dst"), _ILLEGAL, ids=[f"{s}-to-{d}" for s, d in _ILLEGAL])
def test_given_an_order_when_it_takes_an_illegal_move_then_it_is_refused_and_keeps_its_status(
    world: World, src: str, dst: str
) -> None:
    order_id = order_in(world, src)

    with raises_code("TRANSITION_NOT_ALLOWED"):
        move_to(world, order_id, dst)

    assert world.manager.get(WorkOrder, order_id).status == src


def test_given_a_done_order_when_triaged_then_the_refusal_names_both_statuses(
    world: World,
) -> None:
    with pytest.raises(Exception, match="done") as refused:
        world.manager.execute(Triage(order_id=world.orders["a_done"]))

    assert getattr(refused.value, "code", None) == "TRANSITION_NOT_ALLOWED"
    assert "done" in str(refused.value)
    assert "triaged" in str(refused.value)


# -- guarantee 3: scheduling needs a technician -----------------------------------------------


def test_given_a_triaged_order_when_scheduled_without_a_technician_then_it_is_refused(
    world: World,
) -> None:
    order_id = world.orders["a_triaged"]

    with pytest.raises(ValidationError):
        Schedule.model_validate({"order_id": order_id})
    with raises_code("INVALID_PARAMS"):
        world.manager.execute("schedule", {"order_id": order_id})

    assert world.manager.get(WorkOrder, order_id).status == "triaged"
    assert world.manager.traverse(order_technician, order_id) == []


def test_given_a_triaged_order_when_scheduled_with_an_unknown_technician_then_it_is_refused(
    world: World,
) -> None:
    order_id = world.orders["a_triaged"]

    with raises_code("PRECONDITION_FAILED"):
        world.manager.execute(Schedule(order_id=order_id, technician_id="nobody"))

    assert world.manager.get(WorkOrder, order_id).status == "triaged"


def test_given_a_triaged_order_when_scheduled_then_exactly_one_technician_is_linked(
    world: World,
) -> None:
    order_id = world.orders["a_triaged"]

    world.manager.execute(Schedule(order_id=order_id, technician_id=world.ids["tech_a0_id"]))

    assigned = world.manager.traverse(order_technician, order_id)
    assert [tech.id for tech in assigned] == ["A-tech-0"]
    assert world.manager.get(WorkOrder, order_id).status == "scheduled"


def test_given_a_technician_from_another_site_when_scheduling_then_it_is_refused_and_nothing_is_linked(
    world: World,
) -> None:
    order_id = world.orders["a_triaged"]

    with raises_code("PRECONDITION_FAILED"):
        world.manager.execute(Schedule(order_id=order_id, technician_id=world.ids["tech_b0_id"]))

    assert world.manager.traverse(order_technician, order_id) == []
    assert world.manager.get(WorkOrder, order_id).status == "triaged"


# -- guarantee 4: completion tells the business -----------------------------------------------


def test_given_a_scheduled_order_when_completed_then_a_completion_fact_names_the_asset(
    world: World,
) -> None:
    order_id = order_in(world, "scheduled")
    before = len(world.manager.events(WorkOrderCompleted))

    world.manager.execute(Complete(order_id=order_id))

    records = world.manager.events(WorkOrderCompleted)
    assert len(records) == before + 1
    new = records[-1]
    assert new.about_id == order_id
    assert new.payload.asset_id == world.ids["asset_a0_id"]


def test_given_a_refused_completion_when_events_are_read_then_no_fact_was_emitted(
    world: World,
) -> None:
    order_id = order_in(world, "reported")
    before = len(world.manager.events(WorkOrderCompleted))

    with raises_code("TRANSITION_NOT_ALLOWED"):
        world.manager.execute(Complete(order_id=order_id))

    assert len(world.manager.events(WorkOrderCompleted)) == before


def test_given_the_completed_orders_when_events_are_read_then_each_names_its_asset(
    world: World,
) -> None:
    facts = world.manager.events(WorkOrderCompleted)

    assert sorted(fact.payload.asset_id for fact in facts) == ["A-asset-0", "B-asset-0"]


# -- guarantee 5: deadlines and overdue --------------------------------------------------------


@pytest.mark.parametrize("priority", list(DEADLINES))
def test_given_a_priority_when_a_fault_is_reported_then_the_deadline_is_set_from_it(
    world: World, priority: str
) -> None:
    order_id = report(world, priority=priority)

    order = world.manager.get(WorkOrder, order_id)
    assert order.response_deadline == FIXTURE_NOW + DEADLINES[priority]


def test_given_orders_seeded_now_when_overdue_is_asked_then_nothing_is_overdue(
    world: World,
) -> None:
    assert world.technician("A").call_function(SiteQuestion(site_id=world.ids["site_a_id"])) == []


def test_given_two_days_have_passed_when_overdue_is_asked_then_only_open_late_orders_are_listed(
    world: World,
) -> None:
    world.clock.set(FIXTURE_NOW + timedelta(days=2))

    late_a = world.technician("A").call_function(SiteQuestion(site_id=world.ids["site_a_id"]))
    late_b = world.manager.call_function(SiteQuestion(site_id=world.ids["site_b_id"]))

    assert {row["id"] for row in late_a} == {world.orders["a_reported"], world.orders["a_triaged"]}
    assert {row["id"] for row in late_b} == {world.orders["b_reported"], world.orders["b_triaged"]}
    assert all(row["status"] in OPEN_STATUSES for row in late_a + late_b)


def test_given_a_late_order_when_it_is_cancelled_then_it_is_no_longer_overdue(
    world: World,
) -> None:
    world.clock.set(FIXTURE_NOW + timedelta(days=2))
    site = SiteQuestion(site_id=world.ids["site_a_id"])
    assert world.manager.call_function(site)

    world.manager.execute(Cancel(order_id=world.orders["a_reported"]))
    world.manager.execute(Cancel(order_id=world.orders["a_triaged"]))

    assert world.manager.call_function(site) == []


# -- guarantee 6: a catalogue price change leaves past usage alone ----------------------------


def test_given_recorded_usage_when_the_catalogue_price_changes_then_past_usage_keeps_its_cost(
    world: World,
) -> None:
    order_id = world.orders["a_scheduled"]
    (old,) = world.manager.traverse(usage_order, order_id, reverse=True)
    assert old.unit_cost == 100

    world.manager.ingest(
        "Part",
        [{"id": "part-0", "part_number": "P-0", "name": "Seal 0", "unit_cost": 999}],
        _SRC,
    )

    assert world.manager.get(Part, "part-0").unit_cost == 999
    assert world.manager.get(PartsUsage, old.id).unit_cost == 100


def test_given_a_new_price_when_parts_are_used_again_then_only_the_new_record_carries_it(
    world: World,
) -> None:
    order_id = world.orders["a_scheduled"]
    world.manager.ingest(
        "Part",
        [{"id": "part-0", "part_number": "P-0", "name": "Seal 0", "unit_cost": 999}],
        _SRC,
    )

    world.manager.execute(RecordPartsUsed(order_id=order_id, part_id="part-0", quantity=1))

    costs = sorted(u.unit_cost for u in world.manager.traverse(usage_order, order_id, reverse=True))
    assert costs == [100, 999]


# -- guarantee 7: cost is derived from usage ---------------------------------------------------


def _expected_cost(world: World, site: str) -> dict[str, int]:
    expected: dict[str, int] = {}
    for asset in world.manager.list(Asset, limit=None):
        if not asset.id.startswith(site):
            continue
        usages = [
            usage
            for order in world.manager.traverse(order_asset, asset.id, reverse=True)
            for usage in world.manager.traverse(usage_order, order.id, reverse=True)
        ]
        expected[asset.id] = sum(u.quantity * u.unit_cost for u in usages)
    return expected


def test_given_recorded_usage_when_cost_is_asked_then_it_equals_quantity_times_captured_cost(
    world: World,
) -> None:
    cost = world.manager.call_function(MaintenanceCost(site_id=world.ids["site_a_id"]))

    assert cost == _expected_cost(world, "A")
    assert cost == {"A-asset-0": 200, "A-asset-1": 0, "A-asset-2": 200}


def test_given_more_usage_when_cost_is_asked_again_then_it_follows_the_records(
    world: World,
) -> None:
    site = MaintenanceCost(site_id=world.ids["site_a_id"])
    world.manager.execute(
        RecordPartsUsed(order_id=world.orders["a_scheduled"], part_id="part-4", quantity=2)
    )

    cost = world.manager.call_function(site)

    assert cost["A-asset-2"] == 200 + 2 * 500
    assert cost == _expected_cost(world, "A")


def test_given_the_schema_when_inspected_then_no_object_stores_a_cost_total() -> None:
    for name, object_type in ontology.registry.object_types.items():
        assert not [p for p in object_type.properties if "total" in p.name.lower()], name


# -- guarantee 8: the AI assistant never sees contact details ---------------------------------


def test_given_an_ai_assistant_when_it_reads_technicians_then_phone_and_email_are_withheld(
    world: World,
) -> None:
    technicians = world.assistant("A").list(Technician, limit=None)

    assert {tech.id for tech in technicians} == {"A-tech-0", "A-tech-1"}
    assert all(tech.phone is None and tech.email is None for tech in technicians)
    assert all(tech.name for tech in technicians)


def test_given_an_ai_assistant_when_it_reads_one_technician_then_phone_and_email_are_withheld(
    world: World,
) -> None:
    technician = world.assistant("A").get(Technician, "A-tech-0")

    assert technician is not None
    assert (technician.phone, technician.email) == (None, None)


def test_given_an_ai_assistant_when_it_asks_about_another_site_then_it_is_refused(
    world: World,
) -> None:
    with raises_code("VISIBILITY_DENIED"):
        world.assistant("A").call_function(SiteQuestion(site_id=world.ids["site_b_id"]))

    with raises_code("VISIBILITY_DENIED"):
        world.assistant("A").get(Technician, "B-tech-0")


def test_given_staff_when_they_read_technicians_then_they_see_phone_and_email(
    world: World,
) -> None:
    for reader in (world.technician("A"), world.manager):
        tech = reader.get(Technician, "A-tech-0")
        assert tech is not None
        assert (tech.phone, tech.email) == ("03-5555-0000", "tech-a0@example.invalid")


# -- guarantee 9: site isolation ---------------------------------------------------------------


def test_given_site_a_staff_when_they_read_then_they_see_only_site_a_records(
    world: World,
) -> None:
    tech = world.technician("A")

    orders = tech.list(WorkOrder, limit=None)
    assets = tech.list(Asset, limit=None)
    usages = tech.list(PartsUsage, limit=None)

    assert {o.id for o in orders} == {
        world.orders[k] for k in world.orders if k.startswith("a_")
    }
    assert {a.id for a in assets} == {"A-asset-0", "A-asset-1", "A-asset-2"}
    assert len(usages) == 2
    for usage in usages:
        (order,) = tech.traverse(usage_order, usage.id)
        assert order.id in {world.orders["a_scheduled"], world.orders["a_done"]}


def test_given_site_a_staff_when_they_reach_for_a_site_b_order_then_it_is_refused(
    world: World,
) -> None:
    tech = world.technician("A")

    with raises_code("VISIBILITY_DENIED"):
        tech.get(WorkOrder, world.orders["b_reported"])
    with raises_code("SCOPE_DENIED"):
        tech.execute(Triage(order_id=world.orders["b_reported"]))
    assert world.manager.get(WorkOrder, world.orders["b_reported"]).status == "reported"


def test_given_a_regional_manager_when_they_read_then_they_see_both_sites(
    world: World,
) -> None:
    assert {a.id[0] for a in world.manager.list(Asset, limit=None)} == {"A", "B"}
    assert len(world.manager.list(WorkOrder, limit=None)) == len(world.orders)
    assert world.manager.call_function(MaintenanceCost(site_id=world.ids["site_b_id"]))


# -- guarantee 10: cancelling releases reservations --------------------------------------------


def test_given_a_scheduled_order_with_a_reservation_when_cancelled_then_it_is_released(
    world: World,
) -> None:
    order_id = order_in(world, "triaged")
    world.manager.execute(
        Schedule(
            order_id=order_id,
            technician_id=world.ids["tech_a0_id"],
            reserve_part_id=world.ids["part_3_id"],
            reserve_quantity=4,
        )
    )
    assert len(world.manager.traverse(reservation_order, order_id, reverse=True)) == 1

    world.manager.execute(Cancel(order_id=order_id))

    assert world.manager.traverse(reservation_order, order_id, reverse=True) == []
    assert world.manager.list(Reservation, where={"quantity": 4}, limit=None) == []


def test_given_the_seeded_cancelled_order_when_read_then_its_reservation_is_gone(
    world: World,
) -> None:
    order_id = world.orders["a_cancelled"]

    assert world.manager.get(WorkOrder, order_id).status == "cancelled"
    assert world.manager.traverse(reservation_order, order_id, reverse=True) == []


def test_given_an_order_without_reservations_when_cancelled_then_other_reservations_stay(
    world: World,
) -> None:
    kept = world.manager.traverse(reservation_order, world.orders["a_scheduled"], reverse=True)

    world.manager.execute(Cancel(order_id=world.orders["a_reported"]))

    assert world.manager.traverse(
        reservation_order, world.orders["a_scheduled"], reverse=True
    ) == kept
    assert len(kept) == 1


def test_given_usage_of_a_reserved_part_when_recorded_then_the_reservation_shrinks(
    world: World,
) -> None:
    order_id = world.orders["a_scheduled"]  # reserved 3 of part-0, 2 already used in the seed

    (reservation,) = world.manager.traverse(reservation_order, order_id, reverse=True)

    assert reservation.quantity == 1


# -- AC4: quantities are rules, not action code ------------------------------------------------


def test_given_a_scheduled_order_when_zero_parts_are_used_then_valid_usage_refuses_it(
    world: World,
) -> None:
    order_id = world.orders["a_scheduled"]

    with pytest.raises(Exception, match="valid_usage") as refused:
        world.manager.execute(RecordPartsUsed(order_id=order_id, part_id="part-0", quantity=0))

    assert getattr(refused.value, "code", None) == "RULE_VIOLATED"
    assert len(world.manager.traverse(usage_order, order_id, reverse=True)) == 1


def test_given_a_triaged_order_when_scheduled_with_a_zero_reservation_then_the_rule_refuses_it(
    world: World,
) -> None:
    order_id = world.orders["a_triaged"]

    with pytest.raises(Exception, match="positive_quantity") as refused:
        world.manager.execute(
            Schedule(
                order_id=order_id,
                technician_id=world.ids["tech_a0_id"],
                reserve_part_id=world.ids["part_0_id"],
                reserve_quantity=0,
            )
        )

    assert getattr(refused.value, "code", None) == "RULE_VIOLATED"
    assert world.manager.get(WorkOrder, order_id).status == "triaged"
    assert world.manager.traverse(order_technician, order_id) == []


def test_given_a_reported_order_when_parts_are_used_then_it_is_refused(world: World) -> None:
    with raises_code("PRECONDITION_FAILED"):
        world.manager.execute(
            RecordPartsUsed(order_id=order_in(world, "reported"), part_id="part-0", quantity=1)
        )


# -- guarantee 11: the tests read as business statements ---------------------------------------


def test_given_this_module_when_its_tests_are_listed_then_each_reads_given_when_then() -> None:
    names = [
        name
        for name, obj in globals().items()
        if name.startswith("test_") and inspect.isfunction(obj)
    ]

    assert len(names) > 30
    for name in names:
        assert name.startswith("test_given_"), name
        assert "_when_" in name and "_then_" in name, name
        assert name.index("_given_") < name.index("_when_") < name.index("_then_"), name


# -- guarantee 12: only the six actions write --------------------------------------------------


def test_given_the_ontology_when_actions_are_listed_then_exactly_the_six_verbs_exist() -> None:
    assert set(ontology.registry.action_types) == {
        "report_fault",
        "triage",
        "schedule",
        "complete",
        "cancel",
        "record_parts_used",
    }


@pytest.mark.parametrize(
    ("type_name", "row"),
    [
        ("WorkOrder", {"id": "wo-x", "fault": "f"}),
        ("PartsUsage", {"id": "u-x", "quantity": 1, "unit_cost": 1}),
        ("Reservation", {"id": "r-x", "quantity": 1}),
    ],
)
def test_given_an_owned_type_when_rows_are_ingested_then_it_is_refused(
    world: World, type_name: str, row: dict[str, object]
) -> None:
    before = len(world.manager.list(type_name, limit=None))

    with raises_code("OWNED_TYPE_REFUSED"):
        world.manager.ingest(type_name, [row], _SRC)

    assert len(world.manager.list(type_name, limit=None)) == before


def test_given_the_owned_types_when_listed_then_they_are_exactly_the_action_written_ones() -> None:
    owned = {n for n, t in ontology.registry.object_types.items() if t.owned}

    assert owned == {"WorkOrder", "PartsUsage", "Reservation"}


def test_given_the_schema_when_inspected_then_technician_is_a_link_and_location_has_no_site() -> (
    None
):
    assert "technician_id" not in WorkOrder.model_fields
    assert set(Location.model_fields) == {"building", "floor"}
    assert ontology.registry.get_link_type("order_technician").owned
    assert ontology.registry.capabilities == {}


# -- three roles --------------------------------------------------------------------------------


def test_given_a_site_a_technician_when_asking_about_both_sites_then_a_is_answered_and_b_refused(
    world: World,
) -> None:
    world.clock.set(FIXTURE_NOW + timedelta(days=2))
    tech = world.technician("A")

    answer = tech.call_function(SiteQuestion(site_id=world.ids["site_a_id"]))
    with raises_code("VISIBILITY_DENIED"):
        tech.call_function(SiteQuestion(site_id=world.ids["site_b_id"]))
    with raises_code("VISIBILITY_DENIED"):
        tech.call_function(MaintenanceCost(site_id=world.ids["site_b_id"]))

    assert {row["id"] for row in answer} == {world.orders["a_reported"], world.orders["a_triaged"]}


def test_given_a_site_a_technician_when_working_a_site_a_order_then_every_verb_is_allowed(
    world: World,
) -> None:
    tech = world.technician("A")
    order_id = tech.execute(
        ReportFault(
            asset_id=world.ids["asset_a1_id"], fault="Leak", severity="minor", priority="low"
        )
    )["order_id"]

    tech.execute(Triage(order_id=order_id))
    tech.execute(Schedule(order_id=order_id, technician_id=world.ids["tech_a0_id"]))
    tech.execute(RecordPartsUsed(order_id=order_id, part_id="part-1", quantity=1))
    tech.execute(Complete(order_id=order_id))

    assert tech.get(WorkOrder, order_id).status == "done"
    cancel_id = order_in(world, "reported")
    tech.execute(Cancel(order_id=cancel_id))
    assert tech.get(WorkOrder, cancel_id).status == "cancelled"


def test_given_a_regional_manager_when_asking_about_both_sites_then_both_answer_with_contacts(
    world: World,
) -> None:
    world.clock.set(FIXTURE_NOW + timedelta(days=2))

    late_a = world.manager.call_function(SiteQuestion(site_id=world.ids["site_a_id"]))
    late_b = world.manager.call_function(SiteQuestion(site_id=world.ids["site_b_id"]))
    technicians = world.manager.list(Technician, limit=None)

    assert len(late_a) == 2 and len(late_b) == 2
    assert len(technicians) == 4
    assert all(t.phone and t.email for t in technicians)


def test_given_a_site_a_assistant_when_asking_then_it_gets_the_technicians_answers_without_contacts(
    world: World,
) -> None:
    world.clock.set(FIXTURE_NOW + timedelta(days=2))
    tech, assistant = world.technician("A"), world.assistant("A")
    site = world.ids["site_a_id"]

    assert assistant.call_function(SiteQuestion(site_id=site)) == tech.call_function(
        SiteQuestion(site_id=site)
    )
    assert assistant.call_function(MaintenanceCost(site_id=site)) == tech.call_function(
        MaintenanceCost(site_id=site)
    )
    technicians = assistant.list(Technician, limit=None)
    assert technicians
    assert all(t.phone is None and t.email is None for t in technicians)
    with raises_code("VISIBILITY_DENIED"):
        assistant.call_function(SiteQuestion(site_id=world.ids["site_b_id"]))


# -- min_n is live ------------------------------------------------------------------------------


def test_given_min_n_two_when_an_aggregate_has_one_contributor_then_it_is_withheld(
    world: World,
) -> None:
    """Prove `min_n=2` is enforced, without adding a Function just for it.

    `count_objects` (MCP) is a visible-row count and is deliberately not min-N-gated, so it cannot
    show the guard. The gated engine paths are `aggregate_objects` over MCP and `aggregate` on the
    client; this test uses the client form, which `aggregate_objects` delegates to. Site A has two
    technicians (passes); narrowing to one technician leaves a single contributor (refused).
    """
    tech = world.technician("A")

    assert tech.aggregate(Technician, func="count") == 2
    with raises_code("MIN_N_VIOLATION"):
        tech.aggregate(Technician, where={"id": "A-tech-0"}, func="count")


def test_given_min_n_two_when_a_single_order_is_aggregated_then_it_is_withheld(
    world: World,
) -> None:
    tech = world.technician("A")

    assert tech.aggregate(WorkOrder, func="count") == 5
    with raises_code("MIN_N_VIOLATION"):
        tech.aggregate(WorkOrder, where={"status": "cancelled"}, func="count")



def test_given_a_scheduled_order_when_scheduled_again_then_it_is_refused_and_keeps_its_technician(
    world: World,
) -> None:
    order_id = world.orders["a_scheduled"]

    with pytest.raises(Exception, match="already has an active link"):
        world.manager.execute(Schedule(order_id=order_id, technician_id=world.ids["tech_a1_id"]))

    assigned = world.manager.traverse(order_technician, order_id)
    assert [tech.id for tech in assigned] == ["A-tech-0"]


def test_given_the_example_ontology_when_validated_then_diagnose_is_clean_and_min_n_is_two() -> (
    None
):
    ontology.validate()

    assert ontology.diagnose() == []
    assert ontology.min_n == 2


def test_given_fixtures_when_work_orders_are_seeded_then_the_five_statuses_all_appear(
    world: World,
) -> None:
    statuses = {world.manager.get(WorkOrder, order_id).status for order_id in world.orders.values()}

    assert statuses == {"reported", "triaged", "scheduled", "done", "cancelled"}
    assert len(world.orders) >= 8


def test_given_fixtures_when_work_orders_are_seeded_then_each_site_has_a_reported_and_a_done_order(
    world: World,
) -> None:
    for site in ("a", "b"):
        assert world.manager.get(WorkOrder, world.orders[f"{site}_reported"]).status == "reported"
        assert world.manager.get(WorkOrder, world.orders[f"{site}_done"]).status == "done"


# -- the example README runs --------------------------------------------------------------------


def test_given_the_example_readme_when_its_python_blocks_run_then_every_claim_holds() -> None:
    readme = Path(__file__).resolve().parent.parent / "examples/maintenance_desk/README.md"
    text = readme.read_text()
    blocks = re.findall(r"^```python\n(.*?)^```$", text, flags=re.DOTALL | re.MULTILINE)

    assert len(blocks) == text.count("```python") == 3
    namespace: dict[str, object] = {"__name__": "readme"}
    for number, code in enumerate(blocks, start=1):
        exec(compile(code, f"examples/maintenance_desk/README.md#{number}", "exec"), namespace)
