"""In-process MCP tests for the room-booking example."""

from __future__ import annotations

import asyncio
import json
from typing import Any, cast

from mcp.server.mcpserver import MCPServer

from examples.room_booking.fixtures import coordinator_consumer
from examples.room_booking.ontology import Booking, booking_session
from examples.room_booking.run_mcp import build_server_and_world


def _call(server: MCPServer, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """Call an MCP tool in-process and decode its payload."""
    result = asyncio.run(server.call_tool(name, arguments))
    if result.structured_content is not None:
        return cast(dict[str, Any], result.structured_content)
    return cast(dict[str, Any], json.loads(result.content[0].text))


def test_given_a_room_with_back_to_back_bookings_when_availability_is_called_then_both_return() -> (
    None
):
    server, world = build_server_and_world()

    payload = _call(
        server,
        "call_function",
        {
            "api_name": "room_availability",
            "params": {
                "room_id": world.ids["room_a_hall_id"],
                "starts_at": "2026-10-06T09:00:00+00:00",
                "ends_at": "2026-10-06T11:00:00+00:00",
            },
        },
    )

    rows = payload["result"]
    assert {row["booking_id"] for row in rows} == {
        world.bookings["a_planning"],
        world.bookings["a_review"],
    }
    planning = next(row for row in rows if row["booking_id"] == world.bookings["a_planning"])
    assert (planning["starts_at"], planning["ends_at"]) == (
        "2026-10-06T09:00:00+00:00",
        "2026-10-06T10:00:00+00:00",
    )


def test_given_an_existing_room_booking_when_an_overlapping_booking_is_attempted_then_it_is_refused() -> (
    None
):
    server, world = build_server_and_world()

    payload = _call(
        server,
        "execute_action",
        {
            "api_name": "book_session",
            "params": {
                "session_id": world.ids["session_a_workshop_id"],
                "room_id": world.ids["room_a_hall_id"],
                "starts_at": "2026-10-06T09:30:00+00:00",
                "ends_at": "2026-10-06T10:30:00+00:00",
            },
        },
    )

    assert payload["error"]["code"] == "PRECONDITION_FAILED"
    message = payload["error"]["message"]
    assert "2026-10-06 09:00:00+00:00" in message
    assert "2026-10-06 10:00:00+00:00" in message


def test_given_a_building_a_coordinator_when_booking_a_building_b_room_then_scope_denied() -> None:
    server, world = build_server_and_world()
    coordinator = world.ontology.bind(world.store, clock=world.clock).for_consumer(
        coordinator_consumer(world.ids, "A")
    )
    session_id = world.ids["session_a_workshop_id"]
    before = coordinator.list(Booking, limit=None)

    assert coordinator.traverse(booking_session, session_id, reverse=True) == []

    payload = _call(
        server,
        "execute_action",
        {
            "api_name": "book_session",
            "params": {
                "session_id": session_id,
                "room_id": world.ids["room_b_hall_id"],
                "starts_at": "2026-10-06T12:00:00+00:00",
                "ends_at": "2026-10-06T13:00:00+00:00",
            },
        },
    )

    assert payload["error"]["code"] == "SCOPE_DENIED"
    assert coordinator.traverse(booking_session, session_id, reverse=True) == []
    assert coordinator.list(Booking, limit=None) == before
    denied = world.store.audit_entries()[-1]
    assert denied.outcome == "denied"
    assert denied.error_code == "SCOPE_DENIED"
    assert denied.writes == []
