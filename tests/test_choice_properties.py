"""Choice properties: `Enum` and `Literal` annotations (#42).

A string-valued `Enum` or `Literal[...]` annotation declares the same
ontology shape as `str` plus `prop(choices=...)`: `type="str"` and the
member values as `choices`. The store keeps the plain string value; a typed
read hands the Enum member (or the `Literal` string) back, so `mypy`
narrows it. Action parameters carry `choices` too, enforced on every
execute path, and MCP describes both.

This file is checked by `mypy --strict` (see the Makefile), so every
`assert_type` below is a static pin as well as a runtime one.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import subprocess
import sys
import textwrap
from enum import Enum, IntEnum, StrEnum
from pathlib import Path
from typing import Any, Literal, assert_type, cast

import pytest
from conftest import raises_code
from mcp.server.mcpserver import MCPServer

from ontary import (
    ActionContext,
    ActionParams,
    Consumer,
    ObjectStore,
    Ontology,
    OntologyObject,
    Source,
    prop,
    target,
)
from ontary.actions import ActionExecutor
from ontary.client import OntologyClient, OntologyRuntime
from ontary.errors import OntaryError, ValidationFailed
from ontary.mcp_server import build_mcp_server
from ontary.meta import (
    ActionParameterDef,
    ActionTypeDef,
    ObjectTypeDef,
    OntologyRegistry,
    PropertyDef,
)
from ontary.scope import ScopePolicy
from ontary.typesys import choice_value

SRC = Source(source_system="seed")


class Status(StrEnum):
    OPEN = "open"
    CLOSED = "closed"


class Priority(Enum):
    """A plain `Enum` whose values are all strings is accepted too."""

    LOW = "low"
    HIGH = "high"


Kind = Literal["bug", "task"]

ontology = Ontology("choice-props", scope_levels=["org"], min_n=1)


@ontology.object(layer="L0", scope="unscoped", owned=True)
class Ticket(OntologyObject):
    id: str = prop(primary_key=True)
    status: Status
    priority: Priority | None = None
    kind: Kind = "task"


@ontology.object(layer="L0", scope="unscoped")
class Incident(OntologyObject):
    """Source-backed, so `ingest` can supply its rows."""

    id: str = prop(primary_key=True)
    status: Status
    priority: Priority | None = None


class TicketRef(ActionParams):
    ticket_id: str = target(Ticket)


class OpenTicketParams(ActionParams):
    kind: Kind
    priority: Priority | None = None


class SetStatusParams(TicketRef):
    status: Status


SEEN: dict[str, Any] = {}


@ontology.action(OpenTicketParams, target=Ticket, roles=["Clerk"], api_name="OpenTicket")
def _open_ticket(ctx: ActionContext, params: OpenTicketParams) -> dict[str, Any]:
    SEEN["open_params"] = params
    ticket = ctx.create(Ticket, status=Status.OPEN, kind=params.kind, priority=params.priority)
    return {"ticket_id": ticket.id}


@ontology.action(SetStatusParams, target=Ticket, roles=["Clerk"], api_name="SetStatus")
def _set_status(ctx: ActionContext, params: SetStatusParams) -> dict[str, Any]:
    SEEN["set_params"] = params
    ticket = ctx.get(Ticket, params.ticket_id)
    assert ticket is not None
    assert_type(ticket.status, Status)
    ticket.status = params.status
    ctx.save(ticket)
    return {}


ontology.validate()


@pytest.fixture
def store() -> ObjectStore:
    store = ObjectStore(ontology.registry)
    store.insert("Ticket", {"id": "t1", "status": "open", "kind": "bug"}, SRC)
    store.insert(
        "Ticket", {"id": "t2", "status": "closed", "priority": "high", "kind": "task"}, SRC
    )
    return store


def _consumer() -> Consumer:
    return Consumer(
        actor_id="clerk-1", role="Clerk", scope_level="org", scope_id="org-1", kind="human"
    )


def _client(store: ObjectStore) -> OntologyClient:
    ids = (f"gen-{n}" for n in itertools.count(1))
    runtime = OntologyRuntime(ontology, store, id_factory=lambda: next(ids))
    return runtime.for_consumer(_consumer())


# -- declaration ---------------------------------------------------------------


def _prop(name: str) -> Any:
    obj_def = ontology.registry.get_object_type("Ticket")
    return next(p for p in obj_def.properties if p.name == name)


def test_str_enum_annotation_declares_str_with_member_values_as_choices() -> None:
    status = _prop("status")
    assert status.type == "str"
    assert status.choices == ("open", "closed")
    assert status.required is True


def test_plain_enum_with_string_values_declares_choices_and_optional() -> None:
    priority = _prop("priority")
    assert priority.type == "str"
    assert priority.choices == ("low", "high")
    assert priority.required is False


def test_literal_annotation_declares_str_with_literal_members_as_choices() -> None:
    kind = _prop("kind")
    assert kind.type == "str"
    assert kind.choices == ("bug", "task")


def test_int_enum_annotation_is_refused_and_names_the_fix() -> None:
    class Level(IntEnum):
        ONE = 1
        TWO = 2

    local = Ontology("int-enum", scope_levels=["org"], min_n=1)
    with raises_code(ValidationFailed, "ONTOLOGY_INVALID") as excinfo:

        @local.object(layer="L0", scope="unscoped")
        class Thing(OntologyObject):
            id: str = prop(primary_key=True)
            level: Level

    message = str(excinfo.value)
    assert "Thing.level" in message
    assert "string" in message


@pytest.mark.parametrize(
    "annotation",
    [
        pytest.param(Literal[1, 2], id="int-literal"),
        pytest.param(Literal["a", 1], id="mixed-literal"),
    ],
)
def test_non_string_literal_annotation_is_refused(annotation: Any) -> None:
    local = Ontology("int-literal", scope_levels=["org"], min_n=1)
    namespace: dict[str, Any] = {
        "__annotations__": {"id": str, "level": annotation},
        "id": prop(primary_key=True),
    }
    Thing = type("Thing", (OntologyObject,), namespace)
    with raises_code(ValidationFailed, "ONTOLOGY_INVALID") as excinfo:
        local.object(layer="L0", scope="unscoped")(Thing)
    assert "string" in str(excinfo.value)


def test_mixed_value_enum_annotation_is_refused() -> None:
    class Mixed(Enum):
        A = "a"
        B = 2

    local = Ontology("mixed-enum", scope_levels=["org"], min_n=1)
    with raises_code(ValidationFailed, "ONTOLOGY_INVALID"):

        @local.object(layer="L0", scope="unscoped")
        class Thing(OntologyObject):
            id: str = prop(primary_key=True)
            level: Mixed


def test_choice_annotation_with_explicit_choices_is_refused() -> None:
    local = Ontology("double-choices", scope_levels=["org"], min_n=1)
    with raises_code(ValidationFailed, "ONTOLOGY_INVALID") as excinfo:

        @local.object(layer="L0", scope="unscoped")
        class Thing(OntologyObject):
            id: str = prop(primary_key=True)
            status: Status = prop(choices=["open", "closed"])

    assert "Thing.status" in str(excinfo.value)
    assert "choices" in str(excinfo.value)


def test_action_params_declare_choices_from_literal_and_enum() -> None:
    open_def = ontology.registry.get_action_type("OpenTicket")
    params = {p.name: p for p in open_def.parameters}
    assert params["kind"].type == "str"
    assert params["kind"].choices == ("bug", "task")
    assert params["kind"].required is True
    assert params["priority"].choices == ("low", "high")
    assert params["priority"].required is False

    set_def = ontology.registry.get_action_type("SetStatus")
    status = next(p for p in set_def.parameters if p.name == "status")
    assert status.choices == ("open", "closed")
    ticket_id = next(p for p in set_def.parameters if p.name == "ticket_id")
    assert ticket_id.choices is None


@pytest.mark.parametrize(
    "annotation",
    [
        pytest.param(Status, id="str-enum"),
        pytest.param(Priority, id="plain-enum"),
        pytest.param(Literal["a", "b"], id="literal"),
    ],
)
def test_choice_annotated_primary_key_is_refused(annotation: Any) -> None:
    """A choice is a value, not an identity: the object ids a store keys on
    are strings, so a choice annotation on the primary key is refused."""
    local = Ontology("choice-pk", scope_levels=["org"], min_n=1)
    namespace: dict[str, Any] = {
        "__annotations__": {"code": annotation},
        "code": prop(primary_key=True),
    }
    Thing = type("Thing", (OntologyObject,), namespace)
    with raises_code(ValidationFailed, "ONTOLOGY_INVALID") as excinfo:
        local.object(layer="L0", scope="unscoped")(Thing)
    message = str(excinfo.value)
    assert "Thing.code" in message
    assert "primary key" in message


def test_int_enum_action_param_is_refused() -> None:
    class Level(IntEnum):
        ONE = 1

    local = Ontology("int-enum-param", scope_levels=["org"], min_n=1)

    @local.object(layer="L0", scope="unscoped", owned=True)
    class Thing(OntologyObject):
        id: str = prop(primary_key=True)

    class BadParams(ActionParams):
        level: Level

    with raises_code(ValidationFailed, "ONTOLOGY_INVALID"):

        @local.action(BadParams, target=Thing, roles=["Clerk"], api_name="Bad")
        def _bad(ctx: ActionContext, params: BadParams) -> dict[str, Any]:
            return {}


# -- reads ---------------------------------------------------------------------


def test_typed_read_hydrates_enum_members_and_literal_strings(store: ObjectStore) -> None:
    ticket = _client(store).get(Ticket, "t2")
    assert ticket is not None
    assert_type(ticket.status, Status)
    assert_type(ticket.priority, Priority | None)
    assert_type(ticket.kind, Literal["bug", "task"])
    assert ticket.status is Status.CLOSED
    assert ticket.priority is Priority.HIGH
    assert ticket.kind == "task"


def test_store_payload_keeps_the_plain_string_value(store: ObjectStore) -> None:
    stored = store.read_current("Ticket", "t2")
    assert stored is not None
    assert stored.payload["priority"] == "high"
    assert type(stored.payload["priority"]) is str


def test_where_filter_accepts_an_enum_member(store: ObjectStore) -> None:
    closed = _client(store).list(Ticket, {"status": Status.CLOSED}, limit=None)
    assert [t.id for t in closed] == ["t2"]
    high = _client(store).list(Ticket, {"priority": Priority.HIGH}, limit=None)
    assert [t.id for t in high] == ["t2"]


def test_grouped_aggregate_keys_are_plain_strings(store: ObjectStore) -> None:
    counts = _client(store).aggregate_by(Ticket, None, "status", func="count")
    assert counts == {"open": 1, "closed": 1}
    assert all(type(key) is str for key in counts)


# -- writes --------------------------------------------------------------------


def test_ingest_accepts_enum_members_and_stores_their_values(store: ObjectStore) -> None:
    _client(store).ingest(
        "Incident", [{"id": "i1", "status": Status.OPEN, "priority": Priority.LOW}], SRC
    )
    stored = store.read_current("Incident", "i1")
    assert stored is not None
    assert stored.payload["status"] == "open"
    assert stored.payload["priority"] == "low"
    assert type(stored.payload["priority"]) is str


def test_ingest_refuses_a_value_outside_the_choices(store: ObjectStore) -> None:
    report = _client(store).ingest(
        "Incident", [{"id": "i2", "status": "pending"}], SRC, on_error="report"
    )
    assert not report.ok
    assert "pending" in str(report.errors[0])
    assert store.read_current("Incident", "i2") is None


def test_ctx_create_and_save_store_enum_values(store: ObjectStore) -> None:
    client = _client(store)
    result = client.execute(OpenTicketParams(kind="bug", priority=Priority.HIGH))
    created = store.read_current("Ticket", result["ticket_id"])
    assert created is not None
    assert created.payload["status"] == "open"
    assert created.payload["priority"] == "high"
    assert type(created.payload["priority"]) is str

    client.execute(SetStatusParams(ticket_id="t1", status=Status.CLOSED))
    saved = store.read_current("Ticket", "t1")
    assert saved is not None
    assert saved.payload["status"] == "closed"
    assert type(saved.payload["status"]) is str


def test_dict_execute_hands_the_handler_enum_members(store: ObjectStore) -> None:
    _client(store).execute("SetStatus", {"ticket_id": "t1", "status": "closed"})
    params = SEEN["set_params"]
    assert params.status is Status.CLOSED


@pytest.mark.parametrize(
    ("api_name", "params"),
    [
        pytest.param("OpenTicket", {"kind": "epic"}, id="literal-param"),
        pytest.param("SetStatus", {"ticket_id": "t1", "status": "pending"}, id="enum-param"),
    ],
)
def test_dict_execute_refuses_a_param_outside_the_choices(
    store: ObjectStore, api_name: str, params: dict[str, Any]
) -> None:
    with raises_code(OntaryError, "INVALID_PARAMS") as excinfo:
        _client(store).execute(api_name, params)
    assert "pending" in str(excinfo.value) or "epic" in str(excinfo.value)
    stored = store.read_current("Ticket", "t1")
    assert stored is not None
    assert stored.payload["status"] == "open"


def test_declared_param_choices_are_enforced_for_a_dict_handler() -> None:
    """A descriptor-declared action with a plain dict handler has no
    pydantic model in front of it, so `ActionParameterDef.choices` is the
    only guard: the refusal is `INVALID_PARAMS`, audited, and the handler
    never runs."""
    registry = OntologyRegistry()
    registry.register_object_type(
        ObjectTypeDef(
            api_name="Gate",
            display_name="Gate",
            description="A gate",
            layer="L0",
            properties=[PropertyDef(name="id", type="str")],
            primary_key="id",
        )
    )
    registry.register_action_type(
        ActionTypeDef(
            api_name="SetMode",
            display_name="Set mode",
            target_type="Gate",
            executable_by_roles=["Clerk"],
            description="Set a gate's mode",
            parameters=[ActionParameterDef(name="mode", type="str", choices=("open", "shut"))],
        )
    )
    store = ObjectStore(registry)
    executor = ActionExecutor(
        store,
        registry,
        ScopePolicy(
            levels=["org"],
            unscoped_types={"Gate"},
            rules={},
            contributor_rules={},
            row_visibility={},
            min_n=1,
        ),
    )
    calls: list[dict[str, Any]] = []

    def _handler(consumer: Consumer, params: dict[str, Any]) -> dict[str, Any]:
        calls.append(params)
        return {}

    executor._register("SetMode", _handler)

    executor.execute(_consumer(), "SetMode", {"mode": "open"})
    assert calls == [{"mode": "open"}]

    class Mode(Enum):
        SHUT = "shut"

    executor.execute(_consumer(), "SetMode", {"mode": Mode.SHUT})
    assert calls[-1] == {"mode": "shut"}
    assert type(calls[-1]["mode"]) is str
    assert store.audit_entries()[-1].params == {"mode": "shut"}

    with raises_code(OntaryError, "INVALID_PARAMS") as excinfo:
        executor.execute(_consumer(), "SetMode", {"mode": "ajar"})
    assert "ajar" in str(excinfo.value)
    assert len(calls) == 2
    last = store.audit_entries()[-1]
    assert last.action == "SetMode"
    assert last.outcome == "error"
    assert last.error_code == "INVALID_PARAMS"


def test_dict_execute_unwraps_a_plain_enum_member_for_a_choices_param(
    store: ObjectStore,
) -> None:
    """Dict form (#42 T3 step 5): a plain `Enum` member (not a `str`) under a
    `choices` param is unwrapped to its value before validation, so the
    action runs, the store and the audit trail both hold the plain string."""
    result = _client(store).execute("OpenTicket", {"kind": "bug", "priority": Priority.HIGH})
    created = store.read_current("Ticket", result["ticket_id"])
    assert created is not None
    assert created.payload["priority"] == "high"
    assert type(created.payload["priority"]) is str
    last = store.audit_entries()[-1]
    assert last.action == "OpenTicket"
    assert last.outcome == "ok"
    assert last.params["priority"] == "high"
    assert type(last.params["priority"]) is str


def test_typed_execute_refuses_a_param_outside_the_choices(store: ObjectStore) -> None:
    """The typed path has pydantic in front of it, but a model built with
    `model_construct` skips that; `ActionParameterDef.choices` still refuses."""
    params = OpenTicketParams.model_construct(kind="epic", priority=None)
    with raises_code(OntaryError, "INVALID_PARAMS") as excinfo:
        _client(store).execute(params)
    assert "epic" in str(excinfo.value)


# -- the one normalisation point (#42 I3) --------------------------------------


def test_choice_value_unwraps_an_enum_member_only_where_choices_are_declared() -> None:
    assert choice_value(Priority.LOW, ("low", "high")) == "low"
    assert type(choice_value(Priority.LOW, ("low", "high"))) is str
    assert choice_value(Priority.LOW, None) is Priority.LOW
    assert choice_value("low", ("low", "high")) == "low"
    assert choice_value(3, None) == 3


def test_owned_default_given_as_an_enum_member_is_stored_as_its_value() -> None:
    local = Ontology("owned-choice-default", scope_levels=["org"], min_n=1)

    @local.object(layer="L0", scope="unscoped", owned={"priority": Priority.LOW})
    class Incident(OntologyObject):
        id: str = prop(primary_key=True)
        status: Status
        priority: Priority | None = None

    local.validate()
    obj_def = local.registry.get_object_type("Incident")
    assert obj_def.owned_property_defaults() == {"priority": "low"}

    store = ObjectStore(local.registry)
    runtime = OntologyRuntime(local, store)
    runtime.for_consumer(_consumer()).ingest("Incident", [{"id": "i1", "status": "open"}], SRC)
    stored = store.read_current("Incident", "i1")
    assert stored is not None
    assert stored.payload["priority"] == "low"
    assert type(stored.payload["priority"]) is str


# -- MCP -----------------------------------------------------------------------


def _call(server: MCPServer, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    result: Any = asyncio.run(server.call_tool(name, arguments))
    if result.structured_content is not None:
        return cast(dict[str, Any], result.structured_content)
    return cast(dict[str, Any], json.loads(result.content[0].text))


def test_mcp_describes_property_choices(store: ObjectStore) -> None:
    server = build_mcp_server(ontology, store, _consumer())
    payload = _call(server, "list_object_types", {})
    ticket = next(d for d in payload["object_types"] if d["api_name"] == "Ticket")
    props = {p["name"]: p for p in ticket["properties"]}
    assert props["status"]["choices"] == ["open", "closed"]
    assert props["kind"]["choices"] == ["bug", "task"]
    assert props["id"]["choices"] is None


def test_mcp_describes_action_param_choices(store: ObjectStore) -> None:
    server = build_mcp_server(ontology, store, _consumer())
    payload = _call(server, "list_action_types", {})
    open_ticket = next(d for d in payload["action_types"] if d["api_name"] == "OpenTicket")
    params = {p["name"]: p for p in open_ticket["parameters"]}
    assert params["kind"]["choices"] == ["bug", "task"]
    assert params["priority"]["choices"] == ["low", "high"]
    set_status = next(d for d in payload["action_types"] if d["api_name"] == "SetStatus")
    set_params = {p["name"]: p for p in set_status["parameters"]}
    assert set_params["status"]["choices"] == ["open", "closed"]
    assert set_params["ticket_id"]["choices"] is None


def test_mcp_round_trips_choice_values(store: ObjectStore) -> None:
    server = build_mcp_server(ontology, store, _consumer())
    result = _call(
        server,
        "execute_action",
        {"api_name": "SetStatus", "params": {"ticket_id": "t1", "status": "closed"}},
    )
    assert result == {"result": {}}
    got = _call(server, "get_object", {"obj_type": "Ticket", "obj_id": "t1"})
    assert got["result"]["payload"]["status"] == "closed"

    refused = _call(
        server,
        "execute_action",
        {"api_name": "SetStatus", "params": {"ticket_id": "t1", "status": "pending"}},
    )
    assert refused["error"]["code"] == "INVALID_PARAMS"


# -- static typing -------------------------------------------------------------


_BAD_SNIPPET = """
from tests.test_choice_properties import Status, Ticket


def use(ticket: Ticket) -> None:
    ticket.kind = "epic"
    ticket.status = "open"
    ticket.status = Status.OPEN
"""


def test_mypy_rejects_a_wrong_choice_value(tmp_path: Path) -> None:
    """A misspelled `Literal` member or a bare string for an Enum field is
    a static error, not only a runtime one."""
    snippet = tmp_path / "bad_choice.py"
    snippet.write_text(textwrap.dedent(_BAD_SNIPPET))
    root = Path(__file__).resolve().parents[1]

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "mypy",
            "--strict",
            "--no-incremental",
            "--config-file",
            str(root / "pyproject.toml"),
            str(snippet),
        ],
        capture_output=True,
        text=True,
        cwd=root,
        env={"MYPYPATH": f"{root / 'src'}:{root}:{root / 'tests'}"},
    )

    out = result.stdout
    assert result.returncode == 1, out
    lines = _BAD_SNIPPET.splitlines()
    kind_line = lines.index('    ticket.kind = "epic"') + 1
    status_line = lines.index('    ticket.status = "open"') + 1
    assert f"bad_choice.py:{kind_line}: error: Incompatible types in assignment" in out
    assert f"bad_choice.py:{status_line}: error: Incompatible types in assignment" in out
    assert out.count(": error:") == 2, out
