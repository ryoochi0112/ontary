"""Parameter descriptions flow from `Field(description=...)` / marker helpers
into the registry and the MCP `list_action_types` / `list_functions` payloads."""

from __future__ import annotations

import asyncio
import json
from typing import Any, cast

from pydantic import Field

from ontary import (
    ActionContext,
    ActionParams,
    Consumer,
    FunctionParams,
    Ontology,
    OntologyObject,
    prop,
    scope_ref,
    target,
)
from ontary.mcp_server import build_mcp_server
from ontary.meta import ActionParameterDef
from ontary.scope import SelfScope
from ontary.store.inmemory import InMemoryStore

ontology = Ontology("param-desc", scope_levels=["org"], min_n=1)


@ontology.object(layer="L0", scope="unscoped")
class Ticket(OntologyObject):
    id: str = prop(primary_key=True)


@ontology.object(layer="L0", scope=[SelfScope(level="org")])
class Team(OntologyObject):
    id: str = prop(primary_key=True)


class EscalateParams(ActionParams):
    ticket_id: str = target(Ticket, description="The ticket to act on.")
    team_id: str = scope_ref(Team, description="The team that owns it.")
    note: str = prop(description="A free-text note.")
    reason: str = Field(description="Why the ticket needs escalation.")
    plain: str


@ontology.action(
    EscalateParams, target=Ticket, roles=["Clerk"], api_name="Escalate",
    description="Escalate a ticket.",
)
def escalate(_ctx: ActionContext, _params: EscalateParams) -> dict[str, Any]:
    return {}


class WindowParams(FunctionParams):
    since: str = Field(description="Start of the window, ISO 8601.")
    until: str


@ontology.function(
    WindowParams, description="Counts things.", input_description="A window.",
    output_description="A count.", api_name="windowCount",
)
def window_count(_query: Any, _params: WindowParams) -> int:
    return 0


ontology.validate()


def _call(name: str) -> dict[str, Any]:
    consumer = Consumer(
        actor_id="clerk", role="Clerk", scope_level="org", scope_id="org-1", kind="human"
    )
    server = build_mcp_server(ontology, InMemoryStore(ontology.registry), consumer)
    result: Any = asyncio.run(server.call_tool(name, {}))
    if result.structured_content is not None:
        return cast(dict[str, Any], result.structured_content)
    return cast(dict[str, Any], json.loads(result.content[0].text))


def _action_params() -> dict[str, dict[str, Any]]:
    payload = _call("list_action_types")
    action = next(a for a in payload["action_types"] if a["api_name"] == "Escalate")
    return {p["name"]: p for p in action["parameters"]}


def _function_params() -> dict[str, dict[str, Any]]:
    payload = _call("list_functions")
    fn = next(f for f in payload["functions"] if f["api_name"] == "windowCount")
    return {p["name"]: p for p in fn["parameters"]}


def test_action_field_description_is_published() -> None:
    assert _action_params()["reason"]["description"] == "Why the ticket needs escalation."


def test_function_field_description_is_published() -> None:
    assert _function_params()["since"]["description"] == "Start of the window, ISO 8601."


def test_marker_helpers_carry_description() -> None:
    params = _action_params()
    assert params["ticket_id"]["description"] == "The ticket to act on."
    assert params["team_id"]["description"] == "The team that owns it."
    assert params["note"]["description"] == "A free-text note."


def test_every_param_has_description_key_and_undescribed_is_none() -> None:
    action = _action_params()
    fn = _function_params()
    for params in (action, fn):
        assert all("description" in p for p in params.values())
    assert action["plain"]["description"] is None
    assert fn["until"]["description"] is None


def test_registry_parameters_carry_description() -> None:
    action = ontology.registry.get_action_type("Escalate")
    by_name = {p.name: p for p in action.parameters}
    assert by_name["reason"].description == "Why the ticket needs escalation."
    assert by_name["plain"].description is None
    assert ActionParameterDef(name="x", type="str", required=True).description is None
