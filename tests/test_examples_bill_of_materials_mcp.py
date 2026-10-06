"""In-process MCP tests for the bill-of-materials example."""

from __future__ import annotations

import asyncio
import json
from typing import Any, cast

from mcp.server.mcpserver import MCPServer

from examples.bill_of_materials.ontology import Part
from examples.bill_of_materials.run_mcp import build_server_and_world


def _call(server: MCPServer, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """Call an MCP tool in-process and decode its payload."""
    result = asyncio.run(server.call_tool(name, arguments))
    if result.structured_content is not None:
        return cast(dict[str, Any], result.structured_content)
    return cast(dict[str, Any], json.loads(result.content[0].text))


def test_given_a_loaded_bike_when_explode_bom_is_called_then_the_golden_sample_returns() -> None:
    server, world = build_server_and_world()
    bike = next(part for part in world.client.list(Part, limit=None) if part.name == "Bike")

    payload = _call(
        server,
        "call_function",
        {"api_name": "explode_bom", "params": {"part_id": bike.id}},
    )

    assert payload == {
        "result": {
            "rows": [
                {
                    "level": 1,
                    "part_id": "frame",
                    "part_name": "Frame",
                    "quantity_per_parent": 1,
                    "total_quantity": 1,
                },
                {
                    "level": 1,
                    "part_id": "wheel",
                    "part_name": "Wheel",
                    "quantity_per_parent": 2,
                    "total_quantity": 2,
                },
                {
                    "level": 2,
                    "part_id": "hub",
                    "part_name": "Hub",
                    "quantity_per_parent": 1,
                    "total_quantity": 2,
                },
                {
                    "level": 3,
                    "part_id": "bearing",
                    "part_name": "Bearing",
                    "quantity_per_parent": 2,
                    "total_quantity": 4,
                },
                {
                    "level": 2,
                    "part_id": "spoke",
                    "part_name": "Spoke",
                    "quantity_per_parent": 32,
                    "total_quantity": 64,
                },
            ],
            "cycles": [],
        }
    }


def test_given_an_unknown_part_when_explode_bom_is_called_then_mcp_returns_a_precondition_error() -> (
    None
):
    server, world = build_server_and_world()
    part_ids = {part.id for part in world.client.list(Part, limit=None)}
    bike_id = next(part.id for part in world.client.list(Part, limit=None) if part.name == "Bike")
    unknown_part_id = f"unknown-{bike_id}"
    assert unknown_part_id not in part_ids

    payload = _call(
        server,
        "call_function",
        {"api_name": "explode_bom", "params": {"part_id": unknown_part_id}},
    )

    assert payload["error"]["type"] == "PreconditionFailed"
    assert payload["error"]["code"] == "PRECONDITION_FAILED"
    assert unknown_part_id in payload["error"]["message"]
