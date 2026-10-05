"""Synthetic reference data, three consumers, and work orders created through the actions.

    clock = FixtureClock(FIXTURE_NOW)
    ontology, store = build_ontology(clock=clock)
    ids = load_fixtures(store)
    manager = ontology.bind(store, clock=clock).for_consumer(manager_consumer(ids))
    orders = seed_work_orders(manager, ids)

`load_fixtures` writes reference rows only. Work orders, parts usage and reservations are owned
types, so `seed_work_orders` creates them with the six actions, never with `store.insert`.
"""

from __future__ import annotations

from datetime import UTC, datetime

from ontary import Consumer, OntologyClient, Source, Store

from .ontology import (
    Cancel,
    Complete,
    Priority,
    RecordPartsUsed,
    ReportFault,
    Schedule,
    Severity,
    Triage,
)

_SRC = Source(source_system="synthetic")

# The instant the seeded work orders are reported at; the manager client's clock must read it.
FIXTURE_NOW = datetime(2026, 10, 5, 9, tzinfo=UTC)


class FixtureClock:
    """A settable clock: ontary's FixedClock cannot move (#157) and a store binds one clock."""

    def __init__(self, now: datetime) -> None:
        self._now = _aware(now)

    def __call__(self) -> datetime:
        return self._now

    def set(self, when: datetime) -> None:
        self._now = _aware(when)


def _aware(when: datetime) -> datetime:
    if when.tzinfo is None or when.utcoffset() is None:
        raise ValueError("FixtureClock time must be timezone-aware")
    return when


def load_fixtures(store: Store) -> dict[str, str]:
    """Seed one Region, two Sites, six Assets, four Technicians and five Parts."""
    region = store.insert("Region", {"id": "R", "name": "Eastern region"}, _SRC)
    ids = {"region_id": region}
    for site in ("A", "B"):
        site_id = store.insert("Site", {"id": site, "name": f"Site {site}"}, _SRC)
        store.create_link("site_region", site_id, region)
        ids[f"site_{site.lower()}_id"] = site_id
        for i in range(3):
            asset_id = store.insert(
                "Asset",
                {
                    "id": f"{site}-asset-{i}",
                    "name": f"Pump {i}",
                    "location": {"building": "Plant", "floor": str(i)},
                },
                _SRC,
            )
            store.create_link("asset_site", asset_id, site_id)
            ids[f"asset_{site.lower()}{i}_id"] = asset_id
        for i in range(2):
            tech_id = store.insert(
                "Technician",
                {
                    "id": f"{site}-tech-{i}",
                    "name": f"Technician {site}{i}",
                    "phone": f"03-5555-000{i}",
                    "email": f"tech-{site.lower()}{i}@example.invalid",
                },
                _SRC,
            )
            store.create_link("technician_site", tech_id, site_id)
            ids[f"tech_{site.lower()}{i}_id"] = tech_id
    for i in range(5):
        ids[f"part_{i}_id"] = store.insert(
            "Part",
            {
                "id": f"part-{i}",
                "part_number": f"P-{i}",
                "name": f"Seal {i}",
                "unit_cost": (i + 1) * 100,
            },
            _SRC,
        )
    return ids


def manager_consumer(ids: dict[str, str]) -> Consumer:
    """A regional Manager: sees both sites and unredacted technician contact details."""
    return Consumer(
        actor_id="manager",
        role="Manager",
        scope_level="region",
        scope_id=ids["region_id"],
        kind="human",
    )


def technician_consumer(ids: dict[str, str], site: str = "A") -> Consumer:
    """A human Technician confined to one site."""
    return Consumer(
        actor_id=f"technician-{site.lower()}",
        role="Technician",
        scope_level="site",
        scope_id=ids[f"site_{site.lower()}_id"],
        kind="human",
    )


def assistant_consumer(ids: dict[str, str], site: str = "A") -> Consumer:
    """An AI assistant confined to one site: it never reads phone or email."""
    return Consumer(
        actor_id=f"assistant-{site.lower()}",
        role="Assistant",
        scope_level="site",
        scope_id=ids[f"site_{site.lower()}_id"],
        kind="ai",
    )


def seed_work_orders(client: OntologyClient, ids: dict[str, str]) -> dict[str, str]:
    """Create nine work orders through the six actions and return name -> order id.

    `client` must be a manager client whose clock reads `FIXTURE_NOW`. Nothing is overdue at
    that instant; two days later the urgent/high open orders at each site are.
    """

    def report(asset: str, fault: str, priority: Priority, severity: Severity = "major") -> str:
        return str(
            client.execute(
                ReportFault(
                    asset_id=ids[asset],
                    fault=fault,
                    severity=severity,
                    priority=priority,
                )
            )["order_id"]
        )

    def schedule(
        order: str, tech: str, part: int | None = None, quantity: int | None = None
    ) -> None:
        client.execute(
            Schedule(
                order_id=order,
                technician_id=ids[tech],
                reserve_part_id=ids[f"part_{part}_id"] if part is not None else None,
                reserve_quantity=quantity,
            )
        )

    orders: dict[str, str] = {}
    for site in ("a", "b"):
        # reported: urgent, so overdue two days on
        orders[f"{site}_reported"] = report(f"asset_{site}0_id", "Seal leaks", "urgent", "critical")
        # triaged: high priority, overdue two days on
        orders[f"{site}_triaged"] = report(f"asset_{site}1_id", "Bearing noise", "high")
        client.execute(Triage(order_id=orders[f"{site}_triaged"]))
        # scheduled with a reservation and one parts-usage record: low priority, not overdue
        orders[f"{site}_scheduled"] = report(f"asset_{site}2_id", "Belt wear", "low", "minor")
        client.execute(Triage(order_id=orders[f"{site}_scheduled"]))
        schedule(orders[f"{site}_scheduled"], f"tech_{site}0_id", part=0, quantity=3)
        client.execute(
            RecordPartsUsed(
                order_id=orders[f"{site}_scheduled"], part_id=ids["part_0_id"], quantity=2
            )
        )
        # done
        orders[f"{site}_done"] = report(f"asset_{site}0_id", "Coupling worn", "normal")
        client.execute(Triage(order_id=orders[f"{site}_done"]))
        schedule(orders[f"{site}_done"], f"tech_{site}1_id")
        client.execute(
            RecordPartsUsed(order_id=orders[f"{site}_done"], part_id=ids["part_1_id"], quantity=1)
        )
        client.execute(Complete(order_id=orders[f"{site}_done"]))
    # one cancelled order whose reservation is released by the cancel
    orders["a_cancelled"] = report("asset_a1_id", "False alarm", "normal", "minor")
    client.execute(Triage(order_id=orders["a_cancelled"]))
    schedule(orders["a_cancelled"], "tech_a1_id", part=2, quantity=1)
    client.execute(Cancel(order_id=orders["a_cancelled"]))
    return orders
