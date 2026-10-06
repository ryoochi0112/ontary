"""Serve the bill-of-materials ontology over MCP stdio for a planner.

Run it with:

    uv run python -m examples.bill_of_materials.run_mcp
"""

from __future__ import annotations

from mcp.server.mcpserver import MCPServer

from ontary import build_mcp_server

from .fixtures import World, load_world, planner_consumer


def build_server_and_world() -> tuple[MCPServer, World]:
    """Build an MCP server and return the loaded world it serves."""
    world = load_world()
    server = build_mcp_server(
        world.ontology,
        world.store,
        planner_consumer(),
    )
    return server, world


def build_server() -> MCPServer:
    """Build the stdio server over the fixture-loaded master data."""
    server, _world = build_server_and_world()
    return server


def main() -> None:
    build_server().run(transport="stdio")


if __name__ == "__main__":
    main()
