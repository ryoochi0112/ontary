"""MCP struct schema, action validation, reads, and whole-property redaction."""

from __future__ import annotations

import asyncio
import json
from typing import Any, Literal, cast

import pytest
from mcp.server.mcpserver import MCPServer
from pydantic import BaseModel

from ontary import ActionContext, ActionParams, Consumer, Ontology, OntologyObject, Source, prop
from ontary.mcp_server import build_mcp_server
from ontary.meta import Sensitivity
from ontary.store.inmemory import InMemoryStore


class Money(BaseModel):
    value: float
    currency: Literal["JPY", "USD"]


ontology = Ontology("struct-mcp", scope_levels=["org"], min_n=1)


@ontology.object(layer="L0", scope="unscoped")
class Order(OntologyObject):
    id: str = prop(primary_key=True)
    amount: Money
    status: str


@ontology.object(layer="L0", scope="unscoped")
class PrivateOrder(OntologyObject):
    id: str = prop(primary_key=True)
    amount: Money | None = prop(sensitivity=Sensitivity(human_visible=False))
    status: str


class RepriceParams(ActionParams):
    price: Money
    reason: str


@ontology.action(RepriceParams, target=Order, roles=["Clerk"], api_name="Reprice")
def reprice(_ctx: ActionContext, params: RepriceParams) -> dict[str, Any]:
    return {"currency": params.price.currency}


ontology.validate()


@pytest.fixture
def server() -> MCPServer:
    store = InMemoryStore(ontology.registry)
    store.insert("Order", {"id": "o1", "amount": {"value": 100, "currency": "JPY"}, "status": "open"}, Source(source_system="seed"))
    store.insert("PrivateOrder", {"id": "p1", "amount": {"value": 100, "currency": "JPY"}, "status": "open"}, Source(source_system="seed"))
    consumer = Consumer(actor_id="clerk", role="Clerk", scope_level="org", scope_id="org-1", kind="human")
    return build_mcp_server(ontology, store, consumer)


def _call(server: MCPServer, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    result: Any = asyncio.run(server.call_tool(name, arguments))
    if result.structured_content is not None:
        return cast(dict[str, Any], result.structured_content)
    return cast(dict[str, Any], json.loads(result.content[0].text))


FIELDS = [
    {"name": "value", "type": "float", "required": True, "choices": None},
    {"name": "currency", "type": "str", "required": True, "choices": ["JPY", "USD"]},
]


def test_object_type_struct_property_has_golden_fields(server: MCPServer) -> None:
    payload = _call(server, "list_object_types", {})
    order = next(item for item in payload["object_types"] if item["api_name"] == "Order")
    props = {item["name"]: item for item in order["properties"]}
    assert props["amount"] == {
        "name": "amount", "type": "struct", "choices": None, "fields": FIELDS,
        "required": True, "ai_usable": True, "human_visible": True, "scope_level": None,
    }
    assert props["status"] == {
        "name": "status", "type": "str", "choices": None, "fields": None,
        "required": True, "ai_usable": True, "human_visible": True, "scope_level": None,
    }


def test_action_type_struct_parameter_lists_inner_fields(server: MCPServer) -> None:
    payload = _call(server, "list_action_types", {})
    action = next(item for item in payload["action_types"] if item["api_name"] == "Reprice")
    params = {item["name"]: item for item in action["parameters"]}
    assert params["price"] == {
        "name": "price", "type": "struct", "choices": None, "fields": FIELDS,
        "required": True, "refers_to": None, "scope_semantics": None,
    }
    assert params["reason"] == {
        "name": "reason", "type": "str", "choices": None, "fields": None,
        "required": True, "refers_to": None, "scope_semantics": None,
    }


def test_execute_action_accepts_valid_struct_and_refuses_bad_field(server: MCPServer) -> None:
    good = _call(server, "execute_action", {"api_name": "Reprice", "params": {"price": {"value": 50, "currency": "USD"}, "reason": "sale"}})
    assert good == {"result": {"currency": "USD"}}
    bad = _call(server, "execute_action", {"api_name": "Reprice", "params": {"price": {"value": 50, "currency": "EUR"}, "reason": "sale"}})
    assert bad["error"]["code"] == "INVALID_PARAMS"
    assert "price.currency" in bad["error"]["message"]


def test_mcp_read_returns_plain_struct_and_redacts_restricted_struct(server: MCPServer) -> None:
    visible = _call(server, "get_object", {"obj_type": "Order", "obj_id": "o1"})
    assert type(visible["result"]["payload"]["amount"]) is dict
    assert visible["result"]["payload"]["amount"] == {"value": 100.0, "currency": "JPY"}
    hidden = _call(server, "get_object", {"obj_type": "PrivateOrder", "obj_id": "p1"})
    assert "amount" not in hidden["result"]["payload"]
    assert hidden["result"]["payload"]["status"] == "open"
