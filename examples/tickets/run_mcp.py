"""Serve the tickets ontology over MCP stdio.

Run it with:

    uv run python -m examples.tickets.run_mcp
"""

from __future__ import annotations

from mcp.server.auth.provider import AccessToken
from mcp.server.mcpserver import MCPServer

from examples.tickets.fixtures import load_fixtures
from examples.tickets.ontology import build_ontology
from ontary import Consumer, build_mcp_server
from ontary.mcp_server import ConsumerResolver, build_multi_consumer_mcp_server


def build_server() -> MCPServer:
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


def build_multi_consumer_server() -> MCPServer:
    """Build one server whose authenticated agents receive queue-scoped views."""
    ontology, store = build_ontology()
    ids = load_fixtures(store)
    consumers = {
        "billing-agent": Consumer(
            actor_id="billing-agent",
            role="Agent",
            scope_level="queue",
            scope_id=ids["queue_a_id"],
            kind="human",
        ),
        "onboarding-agent": Consumer(
            actor_id="onboarding-agent",
            role="Agent",
            scope_level="queue",
            scope_id=ids["queue_b_id"],
            kind="human",
        ),
    }

    def resolve_consumer(token: AccessToken) -> Consumer | None:
        return consumers.get(token.client_id)

    resolver: ConsumerResolver = resolve_consumer
    return build_multi_consumer_mcp_server(
        ontology,
        store,
        resolve_consumer=resolver,
    )


def main() -> None:
    build_server().run(transport="stdio")


if __name__ == "__main__":
    main()
