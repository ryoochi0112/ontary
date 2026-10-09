# Room booking reference app

`examples/room_booking/` is the third in-repo reference app for `ontary`.
It models room bookings: a session is placed in a room for a time range.
Read it after the [maintenance desk app](../maintenance_desk/README.md) when you want to see a relationship that carries its own facts, and time ranges.

## Domain map

- **Building** is the one scope level.
- **Room** has a `capacity`. It belongs to one Building (`room_building`).
- **Session** is a meeting with `expected_attendees`. It belongs to one Building (`session_building`).
- **Booking** places one Session in one Room (`booking_session`, `booking_room`). It holds `starts_at` and `ends_at`. It is an owned type, so only actions write it.

Session and Room have no time property and no `room_id`.
All times live on Booking.

Three actions are the only writers: `book_session`, `reschedule_session`, and `cancel_booking`.
Two Functions derive answers: `room_availability` and `room_schedule`.
Neither answer is stored.

[`fixtures.py`](fixtures.py) gives a coordinator consumer for each building.
A coordinator sees only the rows of their own building.
See [`ontology.py`](ontology.py) for the full declaration.

## Which feature enforces which guarantee

| Guarantee | Engine feature | Where |
| --- | --- | --- |
| A booking ends after it starts. | `@ontology.rule` | `ends_after_start` in `ontology.py` |
| Only actions write bookings. | `owned=True` | `Booking` |
| A coordinator reads only their own building. | `ScopePolicy` through `ViaLink` | every scoped type |
| A room is never double booked. | Action check | `_check_overlap` |
| Attendees fit the room capacity. | Action check | `book_session`, `reschedule_session` |
| A booking never starts in the past. | Action check | `book_session`, `reschedule_session` |
| A session has one live booking. | Action check | `book_session` |
| A coordinator cannot use a room from another building. | Engine scope check on `target()` and `ref()` | `book_session`, `reschedule_session` |

The fixture clock reads 2026-10-05 08:00 UTC when the world is seeded.
The seeded bookings are on 2026-10-06.
The first block builds the world and reads the two Functions.
`room_availability` and `room_schedule` return `starts_at` and `ends_at` as ISO strings.

```python
from datetime import UTC, date, datetime

from examples.room_booking.fixtures import coordinator_consumer, seed_world
from examples.room_booking.ontology import (
    Booking,
    BookSession,
    CancelBooking,
    RescheduleSession,
    RoomAvailability,
    RoomSchedule,
)

world = seed_world()
client = world.ontology.bind(world.store, clock=world.clock).for_consumer(
    coordinator_consumer(world.ids, "A")
)
hall = world.ids["room_a_hall_id"]


def at(hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 10, 6, hour, minute, tzinfo=UTC)


def refusal(params: object) -> Exception:
    """Run an action that must fail, and return the error."""
    try:
        client.execute(params)
    except Exception as exc:
        return exc
    raise AssertionError("the action was not refused")


window = client.call_function(RoomAvailability(room_id=hall, starts_at=at(9), ends_at=at(11)))
assert [row["starts_at"] for row in window] == [
    "2026-10-06T09:00:00+00:00",
    "2026-10-06T10:00:00+00:00",
]
day = client.call_function(RoomSchedule(room_id=hall, day=date(2026, 10, 6)))
assert [row["title"] for row in day] == ["Planning meeting", "Project review"]
```

## Why Booking is a relationship object

A booking connects a session and a room, and it carries facts: when it starts and when it ends.
A plain link has no place for those facts.
A `room_id` and times on Session would store the same fact twice and would break when a session moves rooms.
So Booking is its own object type, owned by the engine, with one link to each side.
The design guide explains this shape in [Links and object-backed link types](../../docs/ontology-design.md#links-and-object-backed-link-types).

## Overlaps and why they are action checks

An overlap check needs to see the other bookings of the room.
A rule sees one object only, so it cannot do that check (ontary#156).
So `book_session` and `reschedule_session` check overlap in the action.
They read the room's live bookings by traversing `booking_room`.
`ActionContext` has no `where`, so the action filters the bookings in Python.

The engine scope-checks both the `target()` session and the `ref()` room.
A building-A coordinator who passes a building-B room is refused with `SCOPE_DENIED`.

The rule `ends_after_start` is a different case.
It compares two fields of one Booking, so it is a rule.
No action compares the two times.

```python
workshop = world.ids["session_a_workshop_id"]

# The new booking would overlap the 09:00-10:00 and 10:00-11:00 bookings of the hall.
error = refusal(BookSession(session_id=workshop, room_id=hall, starts_at=at(9, 30), ends_at=at(10, 30)))
assert error.code == "PRECONDITION_FAILED"
assert "room already booked" in str(error)

# A room in building B is refused, even though the coordinator named it by id.
error = refusal(
    BookSession(
        session_id=workshop,
        room_id=world.ids["room_b_hall_id"],
        starts_at=at(13),
        ends_at=at(14),
    )
)
assert error.code == "SCOPE_DENIED"

# The rule refuses a range that ends before it starts.
error = refusal(BookSession(session_id=workshop, room_id=hall, starts_at=at(14), ends_at=at(13)))
assert error.code == "RULE_VIOLATED"
assert "ends_after_start" in str(error)
```

## Half-open ranges

Bookings use half-open ranges `[starts_at, ends_at)`.
The start is inclusive and the end is exclusive.
So one booking can end at 10:00 and the next can start at 10:00.
These two bookings do not overlap.
The seeded hall already has such a pair: 09:00-10:00 and 10:00-11:00.

```python
# 11:00 touches the end of the review booking, so this booking is allowed.
booked = client.execute(BookSession(session_id=workshop, room_id=hall, starts_at=at(11), ends_at=at(12)))
first_id = booked["booking_id"]
assert [row["starts_at"] for row in client.call_function(
    RoomAvailability(room_id=hall, starts_at=at(11), ends_at=at(12))
)] == ["2026-10-06T11:00:00+00:00"]

# A session has one live booking, so a second booking points to reschedule_session.
error = refusal(BookSession(session_id=workshop, room_id=hall, starts_at=at(15), ends_at=at(16)))
assert error.code == "PRECONDITION_FAILED"
assert "reschedule_session" in str(error)
```

## Rescheduling and history

`reschedule_session` retires the old Booking and creates a new one.
It never edits a booking in place.
The new booking passes the same checks as a new booking, but it may overlap the old one.
If a check fails, the old booking stays live.
`cancel_booking` retires the booking and closes its links.

History is row history.
The retired row stays in the store with `valid_to` set.
Current reads show only the live booking.
There is no `BookingHistory` type and no status field.

```python
moved = client.execute(RescheduleSession(session_id=workshop, room_id=hall, starts_at=at(12), ends_at=at(13)))
second_id = moved["booking_id"]
assert second_id != first_id

assert client.get(Booking, first_id) is None
assert client.get(Booking, second_id) is not None

retired = world.store.read_last("Booking", first_id)
assert retired is not None
assert retired.lineage.valid_to is not None
assert datetime.fromisoformat(retired.lineage.valid_to) == world.clock()
```

## Time: FixtureClock

A store binds one clock, and `FixedClock` cannot move (ontary#157).
So the example has its own `FixtureClock`, which you can move with `set()`.
`book_session` and `reschedule_session` read the time with `ctx.now()` and refuse a start in the past.
The next block moves the clock past the booking time, and the refusal appears.
It moves the clock back, and the same call succeeds.

```python
client.execute(CancelBooking(session_id=workshop))
assert client.get(Booking, second_id) is None

world.clock.set(at(12, 30))
error = refusal(BookSession(session_id=workshop, room_id=hall, starts_at=at(12), ends_at=at(13)))
assert error.code == "PRECONDITION_FAILED"
assert "past" in str(error)

world.clock.set(datetime(2026, 10, 5, 8, tzinfo=UTC))
again = client.execute(BookSession(session_id=workshop, room_id=hall, starts_at=at(12), ends_at=at(13)))
assert client.get(Booking, again["booking_id"]) is not None
```

## Cost note

The Functions find bookings with a `where` range on `starts_at` and `ends_at`.
Then they traverse `booking_room` and `booking_session` once for each booking row.
That is one traversal per row.
This is fine for an example.
A large deployment should narrow the rows first, or accept the per-row cost.

## Run it

From the repository root, run the tests:

```bash
uv run pytest tests/test_examples_room_booking_e2e.py tests/test_examples_room_booking_mcp.py tests/test_examples_room_booking_docs.py
```

Serve the ontology over MCP stdio for a building-A coordinator:

```bash
uv run python -m examples.room_booking.run_mcp
```
