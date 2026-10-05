"""Checks for the maintenance_desk example: declaration facts and seeded fixtures."""

from __future__ import annotations

from collections import Counter

from examples.maintenance_desk.fixtures import (
    FIXTURE_NOW,
    load_fixtures,
    manager_consumer,
    seed_work_orders,
)
from examples.maintenance_desk.ontology import (
    Location,
    WorkOrder,
    build_ontology,
    ontology,
)
from ontary.testing import FixedClock, SequentialIds


def test_given_the_example_ontology_when_validated_then_diagnose_is_clean_and_min_n_is_two() -> (
    None
):
    ontology.validate()

    assert ontology.diagnose() == []
    assert ontology.min_n == 2


def test_given_fixtures_when_work_orders_are_seeded_then_every_status_appears_at_both_sites() -> (
    None
):
    clock = FixedClock(FIXTURE_NOW)
    onto, store = build_ontology(clock=clock)
    ids = load_fixtures(store)
    manager = onto.bind(store, clock=clock, id_factory=SequentialIds("wo")).for_consumer(
        manager_consumer(ids)
    )

    orders = seed_work_orders(manager, ids)

    statuses = Counter(manager.get(WorkOrder, order_id).status for order_id in orders.values())
    assert set(statuses) == {"reported", "triaged", "scheduled", "done", "cancelled"}
    assert len(orders) >= 8
    for site in ("a", "b"):
        assert manager.get(WorkOrder, orders[f"{site}_reported"]).status == "reported"
        assert manager.get(WorkOrder, orders[f"{site}_done"]).status == "done"


def test_given_the_schema_when_inspected_then_technician_is_a_link_and_location_has_no_site() -> (
    None
):
    assert "technician_id" not in WorkOrder.model_fields
    assert set(Location.model_fields) == {"building", "floor"}
    assert ontology.registry.get_link_type("order_technician").owned
    assert ontology.registry.capabilities == {}
