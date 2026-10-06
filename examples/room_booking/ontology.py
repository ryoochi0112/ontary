"""Room bookings carry the times of a session's placement in a room. Buildings, rooms and
sessions are source-backed reference data; bookings are owned and written through business actions.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, date, datetime, time, timedelta

from ontary import (
    ActionContext,
    ActionError,
    ActionParams,
    BoundQuery,
    Cardinality,
    FunctionParams,
    ObjectStore,
    Ontology,
    OntologyObject,
    SelfScope,
    ViaLink,
    prop,
    ref,
    target,
)

# Default 3 needs justification; 1 disables the guard; 2 still refuses a one-person aggregate.
ontology = Ontology("room_booking", scope_levels=["building"], min_n=2)


@ontology.object(layer="L0", scope=[SelfScope(level="building")])
class Building(OntologyObject):
    id: str = prop(primary_key=True)
    name: str


@ontology.object(
    layer="L0",
    scope=[ViaLink(link_api_name="room_building", direction="from", parent_type="Building")],
)
class Room(OntologyObject):
    id: str = prop(primary_key=True)
    name: str
    capacity: int


@ontology.object(
    layer="L0",
    scope=[ViaLink(link_api_name="session_building", direction="from", parent_type="Building")],
)
class Session(OntologyObject):
    id: str = prop(primary_key=True)
    title: str
    expected_attendees: int


@ontology.object(
    layer="L0",
    owned=True,
    scope=[ViaLink(link_api_name="booking_room", direction="from", parent_type="Room")],
)
class Booking(OntologyObject):
    id: str = prop(primary_key=True)
    starts_at: datetime
    ends_at: datetime


@ontology.rule(Booking, "ends_after_start", message="a booking must end after it starts")
def ends_after_start(booking: Booking) -> bool:
    return booking.ends_at > booking.starts_at


room_building = ontology.link("room_building", Room, Building, Cardinality.MANY_TO_ONE)
session_building = ontology.link("session_building", Session, Building, Cardinality.MANY_TO_ONE)
booking_session = ontology.link(
    "booking_session", Booking, Session, Cardinality.MANY_TO_ONE, owned=True
)
booking_room = ontology.link("booking_room", Booking, Room, Cardinality.MANY_TO_ONE, owned=True)


class BookSession(ActionParams):
    session_id: str = target(Session)
    room_id: str = ref(Room)
    starts_at: datetime
    ends_at: datetime


class RescheduleSession(ActionParams):
    session_id: str = target(Session)
    room_id: str = ref(Room)
    starts_at: datetime
    ends_at: datetime


class CancelBooking(ActionParams):
    session_id: str = target(Session)


def _check_overlap(
    ctx: ActionContext,
    room: Room,
    starts_at: datetime,
    ends_at: datetime,
    excluded_booking_id: str | None = None,
) -> None:
    # ActionContext has no where, so filter the room's live bookings in Python.
    for booking in ctx.traverse(booking_room, room, reverse=True):
        if booking.id != excluded_booking_id and (
            booking.starts_at < ends_at and booking.ends_at > starts_at
        ):
            raise ActionError(
                f"room already booked from {booking.starts_at} to {booking.ends_at}",
                code="PRECONDITION_FAILED",
            )


@ontology.action(BookSession, target=Session, roles=["Coordinator"])
def book_session(ctx: ActionContext, p: BookSession) -> dict[str, str]:
    session = ctx.get(Session, p.session_id)
    room = ctx.get(Room, p.room_id)
    if session is None or room is None:
        raise ActionError("session and room must exist", code="PRECONDITION_FAILED")
    # ref() params are not scope-checked by the engine; enforce the same building here.
    if ctx.traverse(room_building, room)[0].id != ctx.traverse(session_building, session)[0].id:
        raise ActionError("room must be in the session's building", code="PRECONDITION_FAILED")
    if ctx.traverse(booking_session, session, reverse=True):
        raise ActionError(
            "session already booked; use reschedule_session", code="PRECONDITION_FAILED"
        )
    if session.expected_attendees > room.capacity:
        raise ActionError("session exceeds room capacity", code="PRECONDITION_FAILED")
    if p.starts_at < ctx.now():
        raise ActionError("booking cannot start in the past", code="PRECONDITION_FAILED")
    _check_overlap(ctx, room, p.starts_at, p.ends_at)
    booking = ctx.create(Booking, starts_at=p.starts_at, ends_at=p.ends_at)
    ctx.link(booking_session, booking, session)
    ctx.link(booking_room, booking, room)
    return {"booking_id": booking.id}


@ontology.action(RescheduleSession, target=Session, roles=["Coordinator"])
def reschedule_session(ctx: ActionContext, p: RescheduleSession) -> dict[str, str]:
    session = ctx.get(Session, p.session_id)
    if session is None:
        raise ActionError("session must exist", code="PRECONDITION_FAILED")
    bookings = ctx.traverse(booking_session, session, reverse=True)
    if not bookings:
        raise ActionError("session has no live booking", code="PRECONDITION_FAILED")
    old = bookings[0]
    room = ctx.get(Room, p.room_id)
    if room is None:
        raise ActionError("room must exist", code="PRECONDITION_FAILED")
    # ref() params are not scope-checked by the engine; enforce the same building here.
    if ctx.traverse(room_building, room)[0].id != ctx.traverse(session_building, session)[0].id:
        raise ActionError("room must be in the session's building", code="PRECONDITION_FAILED")
    if session.expected_attendees > room.capacity:
        raise ActionError("session exceeds room capacity", code="PRECONDITION_FAILED")
    if p.starts_at < ctx.now():
        raise ActionError("booking cannot start in the past", code="PRECONDITION_FAILED")
    _check_overlap(ctx, room, p.starts_at, p.ends_at, excluded_booking_id=old.id)
    ctx.retire(old)
    booking = ctx.create(Booking, starts_at=p.starts_at, ends_at=p.ends_at)
    ctx.link(booking_session, booking, session)
    ctx.link(booking_room, booking, room)
    return {"booking_id": booking.id}


@ontology.action(CancelBooking, target=Session, roles=["Coordinator"])
def cancel_booking(ctx: ActionContext, p: CancelBooking) -> dict[str, str]:
    session = ctx.get(Session, p.session_id)
    if session is None:
        raise ActionError("session must exist", code="PRECONDITION_FAILED")
    bookings = ctx.traverse(booking_session, session, reverse=True)
    if not bookings:
        raise ActionError("session has no live booking", code="PRECONDITION_FAILED")
    booking = bookings[0]
    ctx.retire(booking)
    return {"booking_id": booking.id}


class RoomAvailability(FunctionParams):
    room_id: str = ref(Room)
    starts_at: datetime
    ends_at: datetime


@ontology.function(
    RoomAvailability, description="Live room bookings overlapping a half-open window."
)
def room_availability(query: BoundQuery, p: RoomAvailability) -> list[dict[str, str]]:
    query.get(Room, p.room_id)  # Explicit room requests must pass the scope gate.
    bookings = query.list(
        Booking,
        where={"starts_at": {"lt": p.ends_at}, "ends_at": {"gt": p.starts_at}},
        limit=None,
    )
    rows: list[dict[str, str]] = []
    for booking in bookings:
        if query.traverse(booking_room, booking.id)[0].id == p.room_id:
            session = query.traverse(booking_session, booking.id)[0]
            rows.append(
                {
                    "booking_id": booking.id,
                    "session_id": session.id,
                    "title": session.title,
                    "starts_at": booking.starts_at.isoformat(),
                    "ends_at": booking.ends_at.isoformat(),
                }
            )
    return rows


class RoomSchedule(FunctionParams):
    room_id: str = ref(Room)
    day: date


@ontology.function(
    RoomSchedule, description="Live room bookings overlapping a UTC day, in start order."
)
def room_schedule(query: BoundQuery, p: RoomSchedule) -> list[dict[str, str]]:
    query.get(Room, p.room_id)
    start = datetime.combine(p.day, time.min, tzinfo=UTC)
    end = start + timedelta(days=1)
    bookings = query.list(
        Booking,
        where={"starts_at": {"lt": end}, "ends_at": {"gt": start}},
        limit=None,
    )
    rows: list[dict[str, str]] = []
    for booking in sorted(bookings, key=lambda b: b.starts_at):
        if query.traverse(booking_room, booking.id)[0].id == p.room_id:
            session = query.traverse(booking_session, booking.id)[0]
            rows.append(
                {
                    "booking_id": booking.id,
                    "session_id": session.id,
                    "title": session.title,
                    "starts_at": booking.starts_at.isoformat(),
                    "ends_at": booking.ends_at.isoformat(),
                }
            )
    return rows


ontology.validate()


def build_ontology(*, clock: Callable[[], datetime] | None = None) -> tuple[Ontology, ObjectStore]:
    """Return the validated ontology paired with a fresh store.

    A store has one clock: pass `clock` to install it, then bind runtimes with the same one.
    """
    store = ObjectStore(ontology.registry)
    if clock is not None:
        store.bind_clock(clock)
    return ontology, store
