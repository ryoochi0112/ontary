import asyncio
from datetime import datetime
from typing import Any, Literal, cast

import pytest
from conftest import raises_code
from pydantic import ValidationError

from ontary import (
    ActionParams,
    FunctionParams,
    Ontology,
    OntologyObject,
    prop,
    ref,
    scope_ref,
    target,
)
from ontary.errors import ValidationFailed
from ontary.functions import BoundQuery
from ontary.mcp_server import build_mcp_server
from ontary.meta import ActionParameterDef, FunctionDef
from ontary.model import _class_stamp
from ontary.store import ObjectStore


def _function_def(
    parameters: list[ActionParameterDef] | None = None,
) -> FunctionDef:
    values: dict[str, Any] = {
        "api_name": "ticketStats",
        "description": "Ticket statistics.",
        "input_description": "A queue id.",
        "output_description": "Statistics.",
    }
    if parameters is not None:
        values["parameters"] = parameters
    return FunctionDef(**values)


def test_function_params_is_a_separate_strict_model() -> None:
    class TicketStatsParams(FunctionParams):
        queue_id: str

    assert not issubclass(TicketStatsParams, ActionParams)
    assert TicketStatsParams(queue_id="q1").queue_id == "q1"
    assert _class_stamp(TicketStatsParams) == (None, None)
    with pytest.raises(ValidationError):
        TicketStatsParams.model_validate({"queue_id": "q1", "unexpected": True})


def test_function_def_defaults_unspecified_parameters_to_no_inputs() -> None:
    assert _function_def().parameters == []
    assert _function_def([]).parameters == []
    assert _function_def([ActionParameterDef(name="queue_id", type="str")]).parameters == [
        ActionParameterDef(name="queue_id", type="str")
    ]


def test_function_def_rejects_explicit_none_parameters() -> None:
    with pytest.raises(ValidationError):
        FunctionDef(
            api_name="ticketStats",
            description="Ticket statistics.",
            input_description="A queue id.",
            output_description="Statistics.",
            parameters=None,  # type: ignore[arg-type]
        )


@pytest.mark.parametrize("semantics", ["target", "scope"])
def test_function_def_refuses_scope_semantics(
    semantics: Literal["target", "scope"],
) -> None:
    parameter = ActionParameterDef(
        name="queue_id",
        type="str",
        refers_to="Queue",
        scope_semantics=semantics,
    )
    with raises_code(ValidationFailed, "ONTOLOGY_INVALID") as exc_info:
        _function_def([parameter])
    message = str(exc_info.value)
    assert "ticketStats" in message
    assert "queue_id" in message
    assert "ref()" in message


def _ontology() -> Ontology:
    return Ontology("functions", scope_levels=["org"], min_n=1)


def test_typed_function_derives_fields_and_stamps_class() -> None:
    ontology = _ontology()

    @ontology.object(layer="L0", scope="unscoped")
    class Queue(OntologyObject):
        id: str = prop(primary_key=True)

    class Details(FunctionParams):
        note: str

    class Params(FunctionParams):
        queue_id: str = ref(Queue)
        limit: int | None
        state: Literal["open", "closed"]
        details: Details

    @ontology.function(Params, api_name="stats")
    def stats(_query: BoundQuery, params: Params) -> int:
        return params.limit or 0

    assert stats.__name__ == "stats"
    assert _class_stamp(Params) == ("stats", ontology.registry)
    fields = ontology.registry.get_function("stats").parameters
    assert fields is not None
    assert [(field.name, field.type, field.required) for field in fields] == [
        ("queue_id", "str", True),
        ("limit", "int", False),
        ("state", "str", True),
        ("details", "struct", True),
    ]
    assert fields[0].refers_to == "Queue"
    assert fields[1].scope_semantics is None
    assert fields[2].choices == ("open", "closed")
    assert fields[3].fields is not None
    assert fields[3].fields[0].name == "note"
    assert Params(queue_id="q1", state="open", details=Details(note="ok")).limit is None


@pytest.mark.parametrize("marker", [target, scope_ref])
def test_function_refuses_action_only_markers(marker: Any) -> None:
    ontology = _ontology()

    @ontology.object(layer="L0", scope="unscoped")
    class Queue(OntologyObject):
        id: str = prop(primary_key=True)

    class Params(FunctionParams):
        queue_id: str = marker(Queue)

    with raises_code(ValidationFailed, "ONTOLOGY_INVALID") as exc_info:
        @ontology.function(Params)
        def stats(_query: BoundQuery, _params: Params) -> None:
            pass

    assert "Params.queue_id" in str(exc_info.value)
    assert "ref()" in str(exc_info.value)
    assert "action-only" in str(exc_info.value)


def test_function_rejects_wrong_reused_and_foreign_params_class() -> None:
    ontology = _ontology()

    class Wrong(ActionParams):
        pass

    with raises_code(ValidationFailed, "ONTOLOGY_INVALID") as wrong:
        @ontology.function(Wrong)  # type: ignore[type-var]
        def wrong_fn(_query: BoundQuery, _params: Wrong) -> None:
            pass
    assert "FunctionParams" in str(wrong.value)

    class Params(FunctionParams):
        value: int

    @ontology.function(Params)
    def first(_query: BoundQuery, _params: Params) -> None:
        pass

    with raises_code(ValidationFailed, "ONTOLOGY_INVALID") as duplicate:
        @ontology.function(Params)
        def second(_query: BoundQuery, _params: Params) -> None:
            pass
    assert "first" in str(duplicate.value)
    assert "already declared" in str(duplicate.value)

    other = _ontology()
    with raises_code(ValidationFailed, "ONTOLOGY_INVALID") as foreign:
        @other.function(Params)
        def third(_query: BoundQuery, _params: Params) -> None:
            pass
    assert "another Ontology" in str(foreign.value)


def test_typed_function_validates_before_handler_and_audits_error(
    make_consumer: Any,
) -> None:
    ontology = _ontology()
    seen: list[FunctionParams] = []

    class Params(FunctionParams):
        count: int
        state: Literal["open", "closed"]
        when: datetime | None = None

    @ontology.function(Params, api_name="stats", audit=True)
    def stats(_query: BoundQuery, params: Params) -> int:
        seen.append(params)
        return params.count

    store = ObjectStore(ontology.registry)
    client = ontology.bind(store).for_consumer(
        make_consumer(actor_id="u1", role="Member", scope_level="org", scope_id="o1", kind="human")
    )
    assert client.call_function("stats", {"count": "2", "state": "open"}) == 2
    assert isinstance(seen[0], Params)
    assert seen[0].count == 2
    instance = Params(count=3, state="closed", when=datetime(2026, 9, 28, 12, 0))
    bound = BoundQuery(client._query, client._consumer, ontology.registry)
    assert ontology.definition.functions.call("stats", bound, instance) == 3
    assert seen[-1] is instance
    assert client.call_function(instance) == 3
    assert seen[-1] is instance
    typed_audit = store.audit_entries()[-1]
    assert typed_audit.action == "stats"
    assert typed_audit.outcome == "ok"
    assert typed_audit.params == {
        "count": 3,
        "state": "closed",
        "when": "2026-09-28T12:00:00",
    }
    before = len(store.audit_entries())
    for invalid in (
        {"state": "open"},
        {"count": "bad", "state": "open"},
        {"count": 1, "state": "other"},
        {"count": 1, "state": "open", "extra": True},
    ):
        with raises_code(ValidationFailed, "INVALID_PARAMS") as exc_info:
            client.call_function("stats", invalid)
        assert "stats" in str(exc_info.value)
    assert len(seen) == 3
    entries = store.audit_entries()[before:]
    assert len(entries) == 4
    assert all(entry.outcome == "error" and entry.error_code == "INVALID_PARAMS" for entry in entries)

    class Other(FunctionParams):
        count: int

    with raises_code(ValidationFailed, "INVALID_PARAMS") as wrong:
        ontology.definition.functions.call(
            "stats", bound, Other(count=1)
        )
    assert "stats" in str(wrong.value)
    assert "expected Params params" in str(wrong.value)
    assert len(seen) == 3

    class Undecorated(Params):
        pass

    with raises_code(ValidationFailed, "UNKNOWN_NAME") as undecorated:
        client.call_function(Undecorated(count=4, state="open"))
    assert "Undecorated" in str(undecorated.value)
    assert "@ontology.function" in str(undecorated.value)
    assert len(seen) == 3

    other_ontology = _ontology()

    class Foreign(FunctionParams):
        count: int

    @other_ontology.function(Foreign, api_name="stats")
    def foreign(_query: BoundQuery, params: Foreign) -> int:
        return params.count

    with raises_code(ValidationFailed, "UNKNOWN_NAME") as foreign_call:
        client.call_function(Foreign(count=5))
    assert "Foreign" in str(foreign_call.value)
    assert "different Ontology" in str(foreign_call.value)
    assert len(seen) == 3


def test_prop_choices_are_checked_for_dict_and_instance_calls(make_consumer: Any) -> None:
    ontology = _ontology()
    seen: list[FunctionParams] = []

    class Params(FunctionParams):
        status: str = prop(choices=["open", "closed"])
        note: str | None = prop(choices=["a", "b"], default=None)

    @ontology.function(Params, api_name="statusSummary", audit=True)
    def status_summary(_query: BoundQuery, params: Params) -> str:
        seen.append(params)
        return params.status

    store = ObjectStore(ontology.registry)
    client = ontology.bind(store).for_consumer(
        make_consumer(actor_id="u1", role="Member", scope_level="org", scope_id="o1", kind="human")
    )

    for invalid_call in (
        lambda: client.call_function("statusSummary", {"status": "bogus"}),
        lambda: client.call_function(Params(status="bogus")),
    ):
        before = len(store.audit_entries())
        with raises_code(ValidationFailed, "INVALID_PARAMS") as exc_info:
            invalid_call()
        message = str(exc_info.value)
        assert "statusSummary" in message
        assert "parameter 'status'" in message
        assert "open" in message and "closed" in message
        assert "bogus" in message
        assert seen == []
        entries = store.audit_entries()[before:]
        assert len(entries) == 1
        assert entries[0].outcome == "error"
        assert entries[0].error_code == "INVALID_PARAMS"

    assert client.call_function("statusSummary", {"status": "open", "note": None}) == "open"
    assert len(seen) == 1
    assert isinstance(seen[0], Params)
    assert seen[0].note is None


def test_no_input_mode(make_consumer: Any) -> None:
    ontology = _ontology()

    @ontology.function(api_name="empty")
    def empty(_query: BoundQuery) -> str:
        return "ok"

    assert ontology.registry.get_function("empty").parameters == []
    ontology.validate()
    store = ObjectStore(ontology.registry)
    consumer = make_consumer(
        actor_id="u1", role="Member", scope_level="org", scope_id="o1", kind="human"
    )
    client = ontology.bind(store).for_consumer(consumer)
    server = build_mcp_server(ontology, store, consumer)
    assert client.call_function("empty") == "ok"
    assert client.call_function("empty", {}) == "ok"
    functions_result = asyncio.run(server.call_tool("list_functions", {}))
    assert functions_result.structured_content is not None
    empty_payload = next(
        fn
        for fn in functions_result.structured_content["functions"]
        if fn["api_name"] == "empty"
    )
    assert empty_payload["parameters"] == []
    with raises_code(ValidationFailed, "INVALID_PARAMS") as exc_info:
        client.call_function("empty", {"value": 3})
    assert "empty" in str(exc_info.value)
    assert "no params" in str(exc_info.value)
    with raises_code(ValidationFailed, "INVALID_PARAMS") as instance_error:
        ontology.definition.functions.call(
            "empty",
            BoundQuery(client._query, client._consumer, ontology.registry),
            FunctionParams(),
        )
    assert "empty" in str(instance_error.value)


def test_mcp_function_payload_parameters_are_never_none(make_consumer: Any) -> None:
    ontology = _ontology()

    @ontology.function(api_name="empty")
    def empty(_query: BoundQuery) -> str:
        return "ok"

    class Params(FunctionParams):
        count: int

    @ontology.function(Params, api_name="withInput")
    def with_input(_query: BoundQuery, params: Params) -> int:
        return params.count

    ontology.validate()
    store = ObjectStore(ontology.registry)
    consumer = make_consumer(
        actor_id="u1", role="Member", scope_level="org", scope_id="o1", kind="human"
    )
    server = build_mcp_server(ontology, store, consumer)
    functions_result = asyncio.run(server.call_tool("list_functions", {}))
    assert functions_result.structured_content is not None
    payloads = functions_result.structured_content["functions"]
    by_name = {fn["api_name"]: fn for fn in payloads}
    assert by_name["empty"]["parameters"] == []
    typed_parameters = by_name["withInput"]["parameters"]
    assert isinstance(typed_parameters, list)
    assert [parameter["name"] for parameter in typed_parameters] == ["count"]
    assert all(fn["parameters"] is not None for fn in payloads)


def test_call_function_rejects_non_function_params_values(make_consumer: Any) -> None:
    ontology = _ontology()

    @ontology.function(api_name="empty")
    def empty(_query: BoundQuery) -> str:
        return "ok"

    class ActionArguments(ActionParams):
        value: str = "value"

    store = ObjectStore(ontology.registry)
    consumer = make_consumer(
        actor_id="u1", role="Member", scope_level="org", scope_id="o1", kind="human"
    )
    client = ontology.bind(store).for_consumer(consumer)
    for invalid_api_name in (ActionArguments(), 42):
        with raises_code(ValidationFailed, "INVALID_PARAMS") as exc_info:
            client.call_function(cast(Any, invalid_api_name))
        assert "an api name str or a FunctionParams instance" in str(exc_info.value)



@pytest.mark.parametrize("shape", ["dict", "defaulted", "variadic", "keyword_only", "extra_keyword"])
def test_function_refuses_handler_without_params_class(shape: str) -> None:
    ontology = _ontology()

    def dict_handler(query: BoundQuery, params: dict[str, Any]) -> None:
        pass

    def defaulted(query: BoundQuery | None = None) -> None:
        pass

    def variadic(*args: Any) -> None:
        pass

    def keyword_only(*, query: BoundQuery) -> None:
        pass

    def extra_keyword(query: BoundQuery, *, extra: int) -> None:
        pass

    handler = {
        "dict": dict_handler,
        "defaulted": defaulted,
        "variadic": variadic,
        "keyword_only": keyword_only,
        "extra_keyword": extra_keyword,
    }[shape]
    with raises_code(ValidationFailed, "ONTOLOGY_INVALID") as exc_info:
        ontology.function(api_name=shape)(handler)
    message = str(exc_info.value)
    assert shape in message
    assert "FunctionParams" in message
    assert "@ontology.function(MyParams)" in message
    assert "(query)" in message
    with raises_code(ValidationFailed, "UNKNOWN_NAME") as missing:
        ontology.registry.get_function(shape)
    assert shape in str(missing.value)
    assert "unregistered function" in str(missing.value)
    assert ontology._function_handlers == {}
