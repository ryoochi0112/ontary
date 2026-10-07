"""MISSING_DESCRIPTION lint: undescribed actions and functions (#65)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, Literal

import pytest
from mcp.server.mcpserver import MCPServer

import ontary.cli as cli
from ontary import ActionParams, Ontology, OntologyObject, prop, target
from ontary.actions import ActionContext
from ontary.functions import BoundQuery
from ontary.mcp_server import build_mcp_server
from ontary.security import Consumer
from ontary.store import ObjectStore

CODE = "MISSING_DESCRIPTION"
Case = Literal["absent", "empty", "blank", "described"]


def _description_kwargs(case: Case) -> dict[str, Any]:
    return {
        "absent": {},
        "empty": {"description": ""},
        "blank": {"description": "   "},
        "described": {"description": "Does the thing."},
    }[case]


def _build(
    *,
    action_kwargs: dict[str, Any],
    function_kwargs: dict[str, Any],
    doc: str | None = None,
) -> Ontology:
    ontology = Ontology("desc", scope_levels=["org"])

    @ontology.object(layer="L0", scope="unscoped")
    class Thing(OntologyObject):
        id: str = prop(primary_key=True)

    class Params(ActionParams):
        thing_id: str = target(Thing)

    def handler(_ctx: ActionContext, params: Params) -> dict[str, str]:
        return {"id": params.thing_id}

    def fn(_query: BoundQuery) -> dict[str, str]:
        return {}

    if doc is not None:
        handler.__doc__ = doc
        fn.__doc__ = doc
    ontology.action(Params, target=Thing, roles=["R"], api_name="A", **action_kwargs)(handler)
    ontology.function(api_name="F", **function_kwargs)(fn)
    return ontology


def _findings(ontology: Ontology, location: str) -> list[Any]:
    return [
        f for f in ontology.diagnose() if f.code == CODE and f.location == location
    ]


@pytest.mark.parametrize("case", ["absent", "empty", "blank", "described"])
def test_action_missing_description(case: Case) -> None:
    ontology = _build(action_kwargs=_description_kwargs(case), function_kwargs={"description": "ok"})
    found = _findings(ontology, "action A")
    if case == "described":
        assert found == []
        return
    assert len(found) == 1
    assert found[0].severity == "warn"
    assert found[0].guide is not None
    assert found[0].guide.endswith("#descriptions-for-agents")
    assert "description=" in found[0].fix_hint
    assert f'accept="{CODE}"' in found[0].fix_hint


@pytest.mark.parametrize("case", ["absent", "empty", "blank", "described"])
def test_function_missing_description(case: Case) -> None:
    ontology = _build(action_kwargs={"description": "ok"}, function_kwargs=_description_kwargs(case))
    found = _findings(ontology, "function F")
    if case == "described":
        assert found == []
        return
    assert len(found) == 1
    assert found[0].severity == "warn"
    assert found[0].guide is not None
    assert found[0].guide.endswith("#descriptions-for-agents")


def _call(server: MCPServer, name: str) -> str:
    result = asyncio.run(server.call_tool(name, {}))
    return json.dumps(result.structured_content) + "".join(
        getattr(part, "text", "") for part in result.content
    )


def test_docstring_is_not_a_description() -> None:
    ontology = _build(action_kwargs={}, function_kwargs={}, doc="Doc text.")
    assert _findings(ontology, "action A")
    assert _findings(ontology, "function F")
    consumer = Consumer(actor_id="a", role="R", scope_level="org", scope_id="x", kind="human")
    server = build_mcp_server(ontology, ObjectStore(ontology.registry), consumer)
    actions = _call(server, "list_action_types")
    functions = _call(server, "list_functions")
    assert "Executes A." in actions
    assert "Computes F." in functions
    assert "Doc text." not in actions
    assert "Doc text." not in functions
    action = ontology.registry.action_types["A"]
    function = ontology.registry.functions["F"]
    assert action.description == "Executes A."
    assert function.description == "Computes F."


def _accepted_ontology() -> Ontology:
    return _build(
        action_kwargs={"accept": CODE},
        function_kwargs={"accept": CODE},
    )


def _target(name: str) -> str:
    return f"{__name__}:{name}"


def test_accept_silences_diagnose() -> None:
    ontology = _accepted_ontology()
    assert not [f for f in ontology.diagnose() if f.code == CODE]


def test_accept_silences_cli(capsys: pytest.CaptureFixture[str]) -> None:
    cli.main(["validate", _target("_accepted_ontology")])
    assert CODE not in capsys.readouterr().out
    cli.main(["validate", _target("_accepted_ontology"), "--json"])
    findings = json.loads(capsys.readouterr().out)
    assert all(item["code"] != CODE for item in findings)


def test_examples_never_mention_the_code() -> None:
    root = Path(__file__).resolve().parents[1] / "examples"
    offenders = [
        str(path)
        for path in root.rglob("*")
        if path.is_file() and CODE in path.read_text(errors="ignore")
    ]
    assert offenders == []
