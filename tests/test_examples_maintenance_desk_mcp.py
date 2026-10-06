"""The maintenance_desk MCP server: a derived Function and a refused action, called as a client."""

from __future__ import annotations

import asyncio
import json
from typing import Any, cast

from examples.maintenance_desk.fixtures import (
    FIXTURE_NOW,
    FixtureClock,
    load_fixtures,
    manager_consumer,
    seed_work_orders,
)
from examples.maintenance_desk.ontology import build_ontology
from examples.maintenance_desk.run_mcp import build_server
from ontary.testing import SequentialIds


def _call(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    server = build_server()
    result: Any = asyncio.run(server.call_tool(name, arguments))
    if result.structured_content is not None:
        return cast(dict[str, Any], result.structured_content)
    return cast(dict[str, Any], json.loads(result.content[0].text))


def _seeded() -> tuple[dict[str, str], dict[str, str]]:
    """The fixture ids and order ids the server seeds, rebuilt on a twin store."""
    clock = FixtureClock(FIXTURE_NOW)
    onto, store = build_ontology(clock=clock)
    ids = load_fixtures(store)
    manager = onto.bind(store, clock=clock, id_factory=SequentialIds("wo")).for_consumer(
        manager_consumer(ids)
    )
    return ids, seed_work_orders(manager, ids)


def test_given_two_days_have_passed_when_overdue_is_called_then_site_a_open_late_orders_return() -> (
    None
):
    ids, orders = _seeded()

    payload = _call(
        "call_function", {"api_name": "overdue", "params": {"site_id": ids["site_a_id"]}}
    )

    rows = payload["result"]
    assert {row["id"] for row in rows} == {orders["a_reported"], orders["a_triaged"]}


def test_given_a_done_order_when_triage_is_executed_then_the_refusal_is_transition_not_allowed() -> (
    None
):
    _, orders = _seeded()

    payload = _call(
        "execute_action", {"api_name": "triage", "params": {"order_id": orders["a_done"]}}
    )

    assert payload["error"]["code"] == "TRANSITION_NOT_ALLOWED"
