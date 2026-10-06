"""Structural guarantees of the room_booking example, as given/when/then tests."""

from examples.room_booking.fixtures import coordinator_consumer, seed_world
from examples.room_booking.ontology import (
    Booking,
    Room,
    Session,
    booking_room,
    booking_session,
    ontology,
)


def test_given_the_ontology_when_validated_then_it_has_no_diagnostics_and_min_n_is_two() -> None:
    ontology.validate()

    assert ontology.diagnose() == []
    assert ontology.min_n == 2


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
