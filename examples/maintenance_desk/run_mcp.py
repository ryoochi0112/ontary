"""Serve the maintenance_desk ontology over MCP stdio, for one site-A technician.

Run it with:

    uv run python -m examples.maintenance_desk.run_mcp
"""

from __future__ import annotations

from datetime import timedelta

from mcp.server.mcpserver import MCPServer

from examples.maintenance_desk.fixtures import (
    FIXTURE_NOW,
    FixtureClock,
    load_fixtures,
    manager_consumer,
    seed_work_orders,
    technician_consumer,
)
from examples.maintenance_desk.ontology import build_ontology
from ontary import build_mcp_server
from ontary.testing import SequentialIds


def build_server() -> MCPServer:
    """Build the fixture-seeded server for one site-A technician.

    One clock serves every runtime, because a store binds a single clock. The manager seeds the
    orders at `FIXTURE_NOW`; the clock then moves two days on, so the urgent and high open orders
    are overdue.
    """
    clock = FixtureClock(FIXTURE_NOW)
    ontology, store = build_ontology(clock=clock)
    ids = load_fixtures(store)
    manager = ontology.bind(store, clock=clock, id_factory=SequentialIds("wo")).for_consumer(
        manager_consumer(ids)
    )
    seed_work_orders(manager, ids)
    clock.set(FIXTURE_NOW + timedelta(days=2))
    return build_mcp_server(ontology, store, technician_consumer(ids, "A"))


def main() -> None:
    build_server().run(transport="stdio")


if __name__ == "__main__":
    main()
