"""MCP visibility and action refusal for declared rules and transitions."""

from __future__ import annotations

import asyncio
import json
from typing import Any, Literal, cast

from mcp.server.mcpserver import MCPServer

from ontary import (
    ActionContext,
    ActionParams,
    Consumer,
    Ontology,
    OntologyObject,
    Source,
    prop,
    target,
)
from ontary.mcp_server import build_mcp_server
from ontary.meta import TransitionDef
from ontary.store.inmemory import InMemoryStore

Status = Literal["pending", "paid", "shipped", "cancelled"]
TRANSITIONS = TransitionDef(
    initial=("pending",),
    moves={
        "pending": ("paid", "cancelled"),
        "paid": ("shipped", "cancelled"),
        "shipped": (),
        "cancelled": (),
    },
)
RULE_MESSAGE = "an order is shipped only after it is paid (paid_at is set)"

ontology = Ontology("rules-transitions-mcp", scope_levels=["org"], min_n=1)


@ontology.object(layer="L0", scope="unscoped", owned=True)
class Order(OntologyObject):
    id: str = prop(primary_key=True)
    status: Status = prop(transitions=TRANSITIONS)
    paid_at: str | None = None


@ontology.rule(Order, "shipped_needs_payment", message=RULE_MESSAGE)
def shipped_needs_payment(order: Order) -> bool:
    return order.status != "shipped" or order.paid_at is not None


class MoveOrderParams(ActionParams):
    order_id: str = target(Order)
    status: Status


@ontology.action(MoveOrderParams, target=Order, roles=["Clerk"], api_name="ShipOrder")
def ship_order(ctx: ActionContext, params: MoveOrderParams) -> dict[str, Any]:
    order = ctx.get(Order, params.order_id)
    assert order is not None
    order.status = params.status
    ctx.save(order)
    return {}


def _server() -> MCPServer:
    store = InMemoryStore(ontology.registry)
    store.insert(
        "Order",
        {"id": "o-1", "status": "pending"},
        Source(source_system="seed"),
    )
    consumer = Consumer(
        actor_id="clerk",
        role="Clerk",
        scope_level="org",
        scope_id="org-1",
        kind="human",
    )
    return build_mcp_server(ontology, store, consumer)


def _call(server: MCPServer, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    result: Any = asyncio.run(server.call_tool(name, arguments))
    if result.structured_content is not None:
        return cast(dict[str, Any], result.structured_content)
    return cast(dict[str, Any], json.loads(result.content[0].text))


def test_list_object_types_exposes_transition_graph_and_rule_summary() -> None:
    payload = _call(_server(), "list_object_types", {})
    order = next(item for item in payload["object_types"] if item["api_name"] == "Order")
    status = next(prop for prop in order["properties"] if prop["name"] == "status")

    assert status["choices"] == ["pending", "paid", "shipped", "cancelled"]
    expected_transitions = {
        "initial": ["pending"],
        "moves": {
            "pending": ["paid", "cancelled"],
            "paid": ["shipped", "cancelled"],
            "shipped": [],
            "cancelled": [],
        },
    }
    assert [
        (prop["name"], prop["transitions"]) for prop in order["properties"]
    ] == [
        ("id", None),
        ("status", expected_transitions),
        ("paid_at", None),
    ]
    assert order["rules"] == [
        {"name": "shipped_needs_payment", "message": RULE_MESSAGE}
    ]
    assert "check" not in order["rules"][0]


def test_execute_action_returns_transition_code_and_golden_message() -> None:
    payload = _call(
        _server(),
        "execute_action",
        {"api_name": "ShipOrder", "params": {"order_id": "o-1", "status": "shipped"}},
    )

    assert payload["error"]["code"] == "TRANSITION_NOT_ALLOWED"
    assert payload["error"]["message"] == (
        "Order 'o-1': status cannot move from 'pending' to 'shipped'; "
        "allowed from 'pending': ['paid', 'cancelled']"
    )
