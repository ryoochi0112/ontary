"""Serve the tickets ontology over MCP stdio.

Run it with:

    uv run python -m examples.tickets.run_mcp
"""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP

from examples.tickets.fixtures import load_fixtures
from examples.tickets.ontology import build_ontology
from ontary import Consumer, build_mcp_server


def build_server() -> FastMCP:
    """Build the fixture-seeded tickets server for one queue-scoped agent."""
    ontology, store = build_ontology()
    ids = load_fixtures(store)
    consumer = Consumer(
        actor_id="ticket-agent",
        role="Agent",
        scope_level="queue",
        scope_id=ids["queue_a_id"],
        kind="human",
    )
    return build_mcp_server(ontology, store, consumer)


def main() -> None:
    build_server().run(transport="stdio")


if __name__ == "__main__":
    main()
