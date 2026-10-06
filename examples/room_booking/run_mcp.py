"""Serve the room-booking ontology over MCP stdio for a building-A coordinator.

Run it with:

    uv run python -m examples.room_booking.run_mcp
"""

from __future__ import annotations

from mcp.server.mcpserver import MCPServer

from ontary import build_mcp_server

from .fixtures import World, coordinator_consumer, seed_world


def build_server_and_world() -> tuple[MCPServer, World]:
    """Build the fixture-seeded MCP server and return its world for in-process callers."""
    world = seed_world()
    server = build_mcp_server(
        world.ontology,
        world.store,
        coordinator_consumer(world.ids, "A"),
    )
    return server, world


def build_server() -> MCPServer:
    """Build the fixture-seeded server for a building-A coordinator."""
    server, _world = build_server_and_world()
    return server


def main() -> None:
    build_server().run(transport="stdio")


if __name__ == "__main__":
    main()
