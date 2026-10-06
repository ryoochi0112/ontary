"""Structural and business guarantees of room_booking through OntologyClient."""

from datetime import UTC, datetime, timedelta

import pytest

from examples.room_booking.fixtures import World, coordinator_consumer, seed_world
from examples.room_booking.ontology import (
    Booking,
    BookSession,
    CancelBooking,
    RescheduleSession,
    Room,
    RoomAvailability,
    RoomSchedule,
    Session,
    booking_room,
    booking_session,
    ontology,
)
from ontary import OntologyClient, Source
from ontary.testing import SequentialIds, raises_code


def test_given_the_ontology_when_validated_then_it_has_no_diagnostics_and_min_n_is_two() -> None:
    ontology.validate()

    assert ontology.diagnose() == []
    assert ontology.min_n == 2


def test_given_the_ontology_when_actions_are_registered_then_they_use_business_api_names() -> None:
    assert set(ontology.registry.action_types) == {
        "book_session",
        "reschedule_session",
        "cancel_booking",
    }


def test_given_booking_when_inspecting_the_model_then_only_it_owns_the_placement_times() -> None:
    booking_type = ontology.registry.get_object_type("Booking")

    assert booking_type.owned is True
    assert {"starts_at", "ends_at"} <= {p.name for p in booking_type.properties}
    assert {"starts_at", "ends_at"} <= Booking.model_fields.keys()
    for cls in (Room, Session):
        properties = {p.name for p in ontology.registry.get_object_type(cls.__name__).properties}
        assert properties.isdisjoint({"room_id", "starts_at", "ends_at"})
        assert cls.model_fields.keys().isdisjoint({"room_id", "starts_at", "ends_at"})


def test_given_reference_fixtures_when_seeding_world_then_each_booking_has_one_session_and_room() -> (
    None
):
    world = seed_world()

    assert set(world.bookings) == {"a_planning", "a_review", "a_standup", "b_briefing"}
    assert len(set(world.bookings.values())) == 4
    for building in ("A", "B"):
        client = world.ontology.bind(world.store, clock=world.clock).for_consumer(
            coordinator_consumer(world.ids, building)
        )
        bookings = client.list(Booking, limit=None)
        assert {b.id for b in bookings} == {
            booking_id
            for key, booking_id in world.bookings.items()
            if key.startswith(f"{building.lower()}_")
        }
        for booking in bookings:
            assert len(client.traverse(booking_session, booking.id)) == 1
            assert len(client.traverse(booking_room, booking.id)) == 1


@pytest.fixture
def world() -> World:
    return seed_world()


@pytest.fixture
def coordinator(world: World) -> OntologyClient:
    return world.ontology.bind(
        world.store, clock=world.clock, id_factory=SequentialIds("test-booking")
    ).for_consumer(coordinator_consumer(world.ids, "A"))


def at(hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 10, 6, hour, minute, tzinfo=UTC)


def schedule(client: OntologyClient, room_id: str) -> list[dict[str, object]]:
    return client.call_function(RoomSchedule(room_id=room_id, day=at(0).date()))


@pytest.mark.parametrize("end", [at(12), at(11)], ids=["zero-length", "reversed"])
def test_given_an_unbooked_session_when_end_is_not_after_start_then_the_rule_refuses_it(
    world: World, coordinator: OntologyClient, end: datetime
) -> None:
    before = coordinator.list(Booking, limit=None)

    with pytest.raises(Exception, match="ends_after_start") as refused:
        coordinator.execute(
            BookSession(
                session_id=world.ids["session_a_workshop_id"],
                room_id=world.ids["room_a_hall_id"],
                starts_at=at(12),
                ends_at=end,
            )
        )

    assert refused.value.code == "RULE_VIOLATED"
    assert coordinator.list(Booking, limit=None) == before


@pytest.mark.parametrize(
    ("start", "end"),
    [(at(9), at(9, 30)), (at(9, 15), at(9, 45)), (at(8, 30), at(9, 30)), (at(9, 30), at(10, 30))],
    ids=["same-start", "contained", "straddles-start", "straddles-end"],
)
def test_given_a_booked_room_when_a_window_overlaps_then_the_conflicting_times_are_named(
    world: World, coordinator: OntologyClient, start: datetime, end: datetime
) -> None:
    room_id = world.ids["room_a_huddle_id"]
    before = schedule(coordinator, room_id)

    with pytest.raises(Exception) as refused:
        coordinator.execute(
            BookSession(
                session_id=world.ids["session_a_workshop_id"],
                room_id=room_id,
                starts_at=start,
                ends_at=end,
            )
        )

    assert refused.value.code == "PRECONDITION_FAILED"
    assert str(at(9)) in str(refused.value)
    assert str(at(10)) in str(refused.value)
    assert schedule(coordinator, room_id) == before


@pytest.mark.parametrize(
    ("start", "end"),
    [(at(8), at(9)), (at(10), at(11))],
    ids=["touches-start", "touches-end"],
)
def test_given_a_booked_room_when_a_window_only_touches_then_booking_succeeds(
    world: World, coordinator: OntologyClient, start: datetime, end: datetime
) -> None:
    room_id = world.ids["room_a_huddle_id"]

    result = coordinator.execute(
        BookSession(
            session_id=world.ids["session_a_workshop_id"],
            room_id=room_id,
            starts_at=start,
            ends_at=end,
        )
    )

    assert {row["booking_id"] for row in schedule(coordinator, room_id)} == {
        world.bookings["a_standup"],
        result["booking_id"],
    }
    booking = coordinator.get(Booking, result["booking_id"])
    assert booking is not None
    assert (booking.starts_at, booking.ends_at) == (start, end)


def test_given_an_already_booked_session_when_booked_again_then_it_points_to_reschedule(
    world: World, coordinator: OntologyClient
) -> None:
    before = coordinator.list(Booking, limit=None)

    with pytest.raises(Exception, match="reschedule_session") as refused:
        coordinator.execute(
            BookSession(
                session_id=world.ids["session_a_review_id"],
                room_id=world.ids["room_a_huddle_id"],
                starts_at=at(12),
                ends_at=at(13),
            )
        )

    assert refused.value.code == "PRECONDITION_FAILED"
    assert coordinator.list(Booking, limit=None) == before


def test_given_twelve_attendees_when_booked_into_huddle_then_capacity_refusal_writes_nothing(
    world: World, coordinator: OntologyClient
) -> None:
    # Free planning's existing booking so capacity is the precondition under test.
    coordinator.execute(CancelBooking(session_id=world.ids["session_a_planning_id"]))
    room_id = world.ids["room_a_huddle_id"]
    before = schedule(coordinator, room_id)
    live = coordinator.list(Booking, limit=None)

    with pytest.raises(Exception, match="capacity") as refused:
        coordinator.execute(
            BookSession(
                session_id=world.ids["session_a_planning_id"],
                room_id=room_id,
                starts_at=at(12),
                ends_at=at(13),
            )
        )

    assert refused.value.code == "PRECONDITION_FAILED"
    assert schedule(coordinator, room_id) == before
    assert coordinator.list(Booking, limit=None) == live


@pytest.mark.parametrize("rescheduling", [False, True], ids=["book", "reschedule"])
def test_given_a_past_start_when_the_clock_moves_earlier_then_the_same_call_succeeds(
    world: World, coordinator: OntologyClient, rescheduling: bool
) -> None:
    params_type = RescheduleSession if rescheduling else BookSession
    session = "review" if rescheduling else "workshop"
    params = params_type(
        session_id=world.ids[f"session_a_{session}_id"],
        room_id=world.ids["room_a_hall_id"],
        starts_at=at(12),
        ends_at=at(13),
    )
    world.clock.set(at(12, 1))
    before = coordinator.list(Booking, limit=None)

    with pytest.raises(Exception, match="past") as refused:
        coordinator.execute(params)

    assert refused.value.code == "PRECONDITION_FAILED"
    assert coordinator.list(Booking, limit=None) == before
    world.clock.set(at(11))
    result = coordinator.execute(params)

    booking = coordinator.get(Booking, result["booking_id"])
    assert booking is not None
    assert booking.starts_at == at(12)
    assert [
        b.id for b in coordinator.traverse(booking_session, params.session_id, reverse=True)
    ] == [result["booking_id"]]


@pytest.mark.parametrize(
    ("room_key", "start", "end"),
    [
        ("room_a_huddle_id", at(10), at(11)),
        ("room_a_hall_id", at(12), at(13)),
        ("room_a_huddle_id", at(12), at(13)),
    ],
    ids=["room", "time", "room-and-time"],
)
def test_given_a_live_booking_when_rescheduled_then_the_old_row_is_retired_and_new_links_move(
    world: World, coordinator: OntologyClient, room_key: str, start: datetime, end: datetime
) -> None:
    old_id = world.bookings["a_review"]
    old = coordinator.get(Booking, old_id)
    world.clock.set(world.clock() + timedelta(hours=1))

    result = coordinator.execute(
        RescheduleSession(
            session_id=world.ids["session_a_review_id"],
            room_id=world.ids[room_key],
            starts_at=start,
            ends_at=end,
        )
    )

    new_id = result["booking_id"]
    assert new_id != old_id
    assert old_id not in {b.id for b in coordinator.list(Booking, limit=None)}
    assert coordinator.get(Booking, old_id) is None
    new = coordinator.get(Booking, new_id)
    assert new is not None
    assert (new.starts_at, new.ends_at) == (start, end)
    assert [r.id for r in coordinator.traverse(booking_room, new_id)] == [world.ids[room_key]]
    assert [
        b.id
        for b in coordinator.traverse(
            booking_session, world.ids["session_a_review_id"], reverse=True
        )
    ] == [new_id]
    assert old_id not in {
        row["booking_id"] for row in schedule(coordinator, world.ids["room_a_hall_id"])
    }
    retired = world.store.read_last("Booking", old_id)
    assert retired is not None and old is not None
    assert retired.lineage.valid_to is not None
    assert datetime.fromisoformat(retired.lineage.valid_to) == world.clock()
    assert retired.payload["starts_at"] == old.starts_at.isoformat()
    assert retired.payload["ends_at"] == old.ends_at.isoformat()


@pytest.mark.parametrize("failure", ["overlap", "capacity", "rule"])
def test_given_a_live_booking_when_rescheduling_fails_then_the_old_booking_and_links_stay_live(
    world: World, coordinator: OntologyClient, failure: str
) -> None:
    session = "planning" if failure == "capacity" else "review"
    room = "huddle" if failure == "capacity" else "hall"
    start, end = (at(9, 30), at(10, 30)) if failure == "overlap" else (at(12), at(13))
    if failure == "rule":
        end = start
    old_id = world.bookings[f"a_{session}"]
    before = coordinator.list(Booking, limit=None)
    old_room = coordinator.traverse(booking_room, old_id)
    old_session = coordinator.traverse(booking_session, old_id)

    with raises_code("RULE_VIOLATED" if failure == "rule" else "PRECONDITION_FAILED"):
        coordinator.execute(
            RescheduleSession(
                session_id=world.ids[f"session_a_{session}_id"],
                room_id=world.ids[f"room_a_{room}_id"],
                starts_at=start,
                ends_at=end,
            )
        )

    assert coordinator.list(Booking, limit=None) == before
    assert coordinator.traverse(booking_room, old_id) == old_room
    assert coordinator.traverse(booking_session, old_id) == old_session
    stored = world.store.read_last("Booking", old_id)
    assert stored is not None and stored.lineage.valid_to is None


@pytest.mark.parametrize(
    ("start", "end"),
    [(at(10), at(11)), (at(10, 15), at(11, 15))],
    ids=["same-slot", "overlaps-only-self"],
)
def test_given_a_live_booking_when_rescheduled_over_itself_then_the_replacement_succeeds(
    world: World, coordinator: OntologyClient, start: datetime, end: datetime
) -> None:
    result = coordinator.execute(
        RescheduleSession(
            session_id=world.ids["session_a_review_id"],
            room_id=world.ids["room_a_hall_id"],
            starts_at=start,
            ends_at=end,
        )
    )

    assert result["booking_id"] != world.bookings["a_review"]
    assert [
        b.id
        for b in coordinator.traverse(
            booking_session, world.ids["session_a_review_id"], reverse=True
        )
    ] == [result["booking_id"]]
    assert coordinator.get(Booking, world.bookings["a_review"]) is None


def test_given_an_unbooked_session_when_rescheduled_then_no_booking_is_written(
    world: World, coordinator: OntologyClient
) -> None:
    before = coordinator.list(Booking, limit=None)

    with pytest.raises(Exception, match="no live booking") as refused:
        coordinator.execute(
            RescheduleSession(
                session_id=world.ids["session_a_workshop_id"],
                room_id=world.ids["room_a_hall_id"],
                starts_at=at(12),
                ends_at=at(13),
            )
        )

    assert refused.value.code == "PRECONDITION_FAILED"
    assert coordinator.list(Booking, limit=None) == before


def test_given_a_live_booking_when_cancelled_then_history_remains_links_close_and_slot_is_free(
    world: World, coordinator: OntologyClient
) -> None:
    session_id = world.ids["session_a_standup_id"]
    room_id = world.ids["room_a_huddle_id"]
    old_id = world.bookings["a_standup"]
    world.clock.set(world.clock() + timedelta(hours=1))

    result = coordinator.execute(CancelBooking(session_id=session_id))

    assert result["booking_id"] == old_id
    assert coordinator.get(Booking, old_id) is None
    assert old_id not in {b.id for b in coordinator.list(Booking, limit=None)}
    assert coordinator.traverse(booking_session, session_id, reverse=True) == []
    assert coordinator.traverse(booking_room, room_id, reverse=True) == []
    assert schedule(coordinator, room_id) == []
    retired = world.store.read_last("Booking", old_id)
    assert retired is not None
    assert retired.lineage.valid_to is not None
    assert datetime.fromisoformat(retired.lineage.valid_to) == world.clock()

    replacement = coordinator.execute(
        BookSession(
            session_id=session_id,
            room_id=room_id,
            starts_at=at(9),
            ends_at=at(10),
        )
    )
    assert replacement["booking_id"] != old_id
    assert [row["booking_id"] for row in schedule(coordinator, room_id)] == [
        replacement["booking_id"]
    ]


@pytest.mark.parametrize(
    ("start", "end", "expected"),
    [
        (at(9, 15), at(9, 45), {"a_planning"}),
        (at(11), at(12), set()),
        (at(8, 30), at(11, 30), {"a_planning", "a_review"}),
    ],
    ids=["hit", "touching-miss", "spans-both"],
)
def test_given_seeded_bookings_when_availability_is_requested_then_exactly_overlaps_are_returned(
    world: World, coordinator: OntologyClient, start: datetime, end: datetime, expected: set[str]
) -> None:
    rows = coordinator.call_function(
        RoomAvailability(room_id=world.ids["room_a_hall_id"], starts_at=start, ends_at=end)
    )

    assert len(rows) == len(expected)
    assert {row["booking_id"] for row in rows} == {world.bookings[key] for key in expected}
    assert {row["session_id"] for row in rows} == {
        world.ids[f"session_{key}_id"] for key in expected
    }
    for row in rows:
        booking = coordinator.get(Booking, row["booking_id"])
        session = coordinator.get(Session, row["session_id"])
        assert booking is not None and session is not None
        assert (row["starts_at"], row["ends_at"], row["title"]) == (
            booking.starts_at.isoformat(),
            booking.ends_at.isoformat(),
            session.title,
        )


def test_given_bookings_created_out_of_time_order_when_scheduled_then_only_that_day_is_sorted(
    world: World, coordinator: OntologyClient
) -> None:
    room_id = world.ids["room_a_hall_id"]
    early = coordinator.execute(
        BookSession(
            session_id=world.ids["session_a_workshop_id"],
            room_id=room_id,
            starts_at=at(8),
            ends_at=at(9),
        )
    )

    rows = schedule(coordinator, room_id)

    assert [row["booking_id"] for row in rows] == [
        early["booking_id"],
        world.bookings["a_planning"],
        world.bookings["a_review"],
    ]
    assert [row["starts_at"] for row in rows] == [
        at(8).isoformat(),
        at(9).isoformat(),
        at(10).isoformat(),
    ]
    assert (
        coordinator.call_function(
            RoomSchedule(room_id=room_id, day=at(0).date() + timedelta(days=1))
        )
        == []
    )


@pytest.mark.parametrize(
    ("session_key", "room_key", "code"),
    [
        ("session_b_checkin_id", "room_b_hall_id", "SCOPE_DENIED"),
        ("session_a_workshop_id", "room_b_hall_id", "PRECONDITION_FAILED"),
    ],
    ids=["b-session", "a-session-b-room"],
)
def test_given_a_building_a_coordinator_when_booking_across_buildings_then_the_request_is_refused(
    world: World, coordinator: OntologyClient, session_key: str, room_key: str, code: str
) -> None:
    before = coordinator.list(Booking, limit=None)

    with raises_code(code):
        coordinator.execute(
            BookSession(
                session_id=world.ids[session_key],
                room_id=world.ids[room_key],
                starts_at=at(12),
                ends_at=at(13),
            )
        )

    assert coordinator.list(Booking, limit=None) == before


def test_given_a_building_a_coordinator_when_reading_building_b_then_schedule_is_invisible(
    world: World, coordinator: OntologyClient
) -> None:
    with raises_code("VISIBILITY_DENIED"):
        coordinator.call_function(
            RoomSchedule(room_id=world.ids["room_b_hall_id"], day=at(0).date())
        )

    assert {b.id for b in coordinator.list(Booking, limit=None)} == {
        world.bookings["a_planning"],
        world.bookings["a_review"],
        world.bookings["a_standup"],
    }


def test_given_owned_booking_when_ingested_then_the_write_is_refused(
    coordinator: OntologyClient,
) -> None:
    before = coordinator.list(Booking, limit=None)

    with raises_code("OWNED_TYPE_REFUSED"):
        coordinator.ingest(
            "Booking",
            [{"id": "ingested-booking", "starts_at": at(12), "ends_at": at(13)}],
            Source(source_system="synthetic"),
        )

    assert coordinator.list(Booking, limit=None) == before
