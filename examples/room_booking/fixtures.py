"""Synthetic reference data and bookings created through the booking action.

    world = seed_world()
    client = world.ontology.bind(world.store, clock=world.clock).for_consumer(
        coordinator_consumer(world.ids)
    )

`load_fixtures` writes reference rows only; `seed_world` seeds owned bookings through
per-building coordinator clients sharing one movable clock.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from ontary import Consumer, ObjectStore, Ontology, OntologyClient, Source, Store
from ontary.testing import SequentialIds

from .ontology import BookSession, Building, build_ontology

_SRC = Source(source_system="synthetic")
FIXTURE_NOW = datetime(2026, 10, 5, 8, tzinfo=UTC)


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
    """Insert two buildings, three rooms and six sessions, with their source links."""
    ids: dict[str, str] = {}
    for building in ("A", "B"):
        ids[f"building_{building.lower()}_id"] = store.insert(
            "Building", {"id": building, "name": f"Building {building}"}, _SRC
        )
    for building, name, capacity in (("A", "huddle", 4), ("A", "hall", 30), ("B", "hall", 30)):
        room_id = store.insert(
            "Room",
            {"id": f"{building}-{name}", "name": f"{building}-{name}", "capacity": capacity},
            _SRC,
        )
        store.create_link("room_building", room_id, ids[f"building_{building.lower()}_id"])
        ids[f"room_{building.lower()}_{name}_id"] = room_id
    for building, name, title, attendees in (
        ("A", "planning", "Planning meeting", 12),
        ("A", "review", "Project review", 4),
        ("A", "standup", "Team standup", 3),
        ("A", "workshop", "Small workshop", 4),
        ("B", "briefing", "Building B briefing", 10),
        ("B", "checkin", "Building B check-in", 3),
    ):
        session_id = store.insert(
            "Session",
            {"id": f"{building}-{name}", "title": title, "expected_attendees": attendees},
            _SRC,
        )
        store.create_link("session_building", session_id, ids[f"building_{building.lower()}_id"])
        ids[f"session_{building.lower()}_{name}_id"] = session_id
    return ids


def coordinator_consumer(ids: dict[str, str], building: str = "A") -> Consumer:
    """A human coordinator confined to one building."""
    return Consumer(
        actor_id=f"coordinator-{building.lower()}",
        role="Coordinator",
        scope_level="building",
        scope_id=ids[f"building_{building.lower()}_id"],
        kind="human",
    )


def seed_bookings(client: OntologyClient, ids: dict[str, str]) -> dict[str, str]:
    """Book the calling coordinator's visible building on 2026-10-06.

    The clock must precede the bookings. A-hall has a back-to-back pair; a separate
    building-B coordinator seeds B-hall through this same helper.
    """
    visible_buildings = {building.id for building in client.list(Building, limit=None)}
    bookings: dict[str, str] = {}
    for building, session, room, start, end in (
        ("a", "planning", "hall", 9, 10),
        ("a", "review", "hall", 10, 11),
        ("a", "standup", "huddle", 9, 10),
        ("b", "briefing", "hall", 9, 10),
    ):
        if ids[f"building_{building}_id"] not in visible_buildings:
            continue
        result = client.execute(
            BookSession(
                session_id=ids[f"session_{building}_{session}_id"],
                room_id=ids[f"room_{building}_{room}_id"],
                starts_at=datetime(2026, 10, 6, start, tzinfo=UTC),
                ends_at=datetime(2026, 10, 6, end, tzinfo=UTC),
            )
        )
        bookings[f"{building}_{session}"] = str(result["booking_id"])
    return bookings


@dataclass
class World:
    """One ontology, store and movable clock, with the seeded reference and booking ids."""

    ontology: Ontology
    store: ObjectStore
    clock: FixtureClock
    ids: dict[str, str]
    bookings: dict[str, str]


def seed_world(clock: FixtureClock | None = None) -> World:
    """Seed the complete example through coordinators for both buildings."""
    if clock is None:
        clock = FixtureClock(FIXTURE_NOW)
    ontology, store = build_ontology(clock=clock)
    ids = load_fixtures(store)
    runtime = ontology.bind(store, clock=clock, id_factory=SequentialIds("bk"))
    bookings: dict[str, str] = {}
    for building in ("A", "B"):
        client = runtime.for_consumer(coordinator_consumer(ids, building))
        bookings.update(seed_bookings(client, ids))
    return World(ontology, store, clock, ids, bookings)
