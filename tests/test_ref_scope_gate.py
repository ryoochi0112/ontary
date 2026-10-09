"""Action refs share the target scope gate on every call path and store backend."""

from __future__ import annotations

import asyncio
import itertools
import json
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, cast

import pytest
from conftest import ConsumerFactory, StoreFactory, raises_code
from pydantic import ConfigDict, Field

from ontary import (
    ActionContext,
    ActionError,
    ActionParams,
    Cardinality,
    Consumer,
    Ontology,
    OntologyClient,
    OntologyObject,
    SelfScope,
    Source,
    ViaLink,
    build_mcp_server,
    prop,
    ref,
    scope_ref,
    target,
)
from ontary.actions import ActionExecutor
from ontary.errors import PermissionDenied, PreconditionFailed
from ontary.meta import OntologyRegistry
from ontary.store import InMemoryStore, Store

_POSTGRES_DSN = os.environ.get("ONTARY_TEST_POSTGRES_DSN")
_schema_counter = itertools.count()
_PATHS = ("typed", "dynamic", "mcp")


def _postgres_store(registry: OntologyRegistry, make_store: StoreFactory) -> Store:
    import psycopg

    assert _POSTGRES_DSN is not None
    schema = f"ontary_test_ref_scope_gate_{next(_schema_counter)}"
    with psycopg.connect(_POSTGRES_DSN, autocommit=True) as setup:
        setup.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        setup.execute(f'CREATE SCHEMA "{schema}"')
    separator = "&" if "?" in _POSTGRES_DSN else "?"
    return make_store(
        registry,
        backend="postgres",
        dsn=f"{_POSTGRES_DSN}{separator}options=-csearch_path%3D{schema}",
    )


@pytest.fixture(
    params=[
        "sqlite",
        "in_memory",
        pytest.param(
            "postgres",
            marks=pytest.mark.skipif(
                not _POSTGRES_DSN, reason="ONTARY_TEST_POSTGRES_DSN is not set"
            ),
        ),
    ]
)
def backend(request: pytest.FixtureRequest) -> str:
    return cast(str, request.param)


@pytest.fixture
def declaration() -> str:
    return "plain"


@dataclass
class _World:
    ontology: Ontology
    store: Store
    consumer: Consumer
    client: OntologyClient
    params_classes: dict[str, type[ActionParams]]
    received: list[str | None]
    factory_calls: list[str]


@pytest.fixture
def world(
    make_consumer: ConsumerFactory,
    make_store: StoreFactory,
    backend: str,
    declaration: str,
) -> _World:
    ontology = Ontology("ref-scope-gate", scope_levels=["building"], min_n=1)

    @ontology.object(layer="L0", scope=[SelfScope(level="building")])
    class Building(OntologyObject):
        id: str = prop(primary_key=True)

    @ontology.object(
        layer="L0",
        scope=[ViaLink(link_api_name="room_building", direction="from", parent_type="Building")],
    )
    class Room(OntologyObject):
        id: str = prop(primary_key=True)

    @ontology.object(
        layer="L0",
        scope=[ViaLink(link_api_name="session_building", direction="from", parent_type="Building")],
    )
    class Session(OntologyObject):
        id: str = prop(primary_key=True)

    @ontology.object(layer="L0", scope="unscoped")
    class Catalog(OntologyObject):
        id: str = prop(primary_key=True)

    ontology.link("room_building", Room, Building, Cardinality.MANY_TO_ONE)
    ontology.link("session_building", Session, Building, Cardinality.MANY_TO_ONE)

    received: list[str | None] = []
    factory_calls: list[str] = []
    params_classes: dict[str, type[ActionParams]] = {}
    room_options: dict[str, Any] = {
        "plain": {},
        "description": {"description": "x"},
        "json_schema_extra": {"json_schema_extra": {"k": "v"}},
        "optional": {},
    }[declaration]

    def _book(ctx: ActionContext, room_id: str | None) -> dict[str, Any]:
        received.append(room_id)
        if room_id is not None and ctx.get(Room, room_id) is None:
            raise ActionError("room must exist", code="PRECONDITION_FAILED")
        return {"ok": room_id}

    if declaration == "optional":
        class OptionalBookParams(ActionParams):
            session_id: str = target(Session)
            room_id: str | None = ref(Room, default=None)

        @ontology.action(OptionalBookParams, target=Session, roles=["Coordinator"], api_name="book")
        def optional_book(ctx: ActionContext, params: OptionalBookParams) -> dict[str, Any]:
            return _book(ctx, params.room_id)

        params_classes["book"] = OptionalBookParams
    else:
        class BookParams(ActionParams):
            session_id: str = target(Session)
            room_id: str = ref(Room, **room_options)

        @ontology.action(BookParams, target=Session, roles=["Coordinator"], api_name="book")
        def book(ctx: ActionContext, params: BookParams) -> dict[str, Any]:
            return _book(ctx, params.room_id)

        params_classes["book"] = BookParams

    class UseCatalogParams(ActionParams):
        catalog_id: str = ref(Catalog)

    @ontology.action(UseCatalogParams, target=Catalog, roles=["Coordinator"], api_name="use_catalog")
    def use_catalog(ctx: ActionContext, params: UseCatalogParams) -> dict[str, str]:
        received.append(params.catalog_id)
        return {"catalog_id": params.catalog_id}

    params_classes["use_catalog"] = UseCatalogParams

    class UseSessionCatalogParams(ActionParams):
        session_id: str = target(Session)
        catalog_id: str = ref(Catalog)

    @ontology.action(
        UseSessionCatalogParams, target=Session, roles=["Coordinator"], api_name="use_session_catalog"
    )
    def use_session_catalog(ctx: ActionContext, params: UseSessionCatalogParams) -> dict[str, str]:
        received.append(params.catalog_id)
        return {"catalog_id": params.catalog_id}

    params_classes["use_session_catalog"] = UseSessionCatalogParams

    class NullableMarkersParams(ActionParams):
        session_id: str | None = target(Session, default=None, required=False)
        building_id: str | None = scope_ref(Building, default=None, required=False)
        room_id: str | None = ref(Room, default=None)

    @ontology.action(
        NullableMarkersParams, target=Session, roles=["Coordinator"], api_name="nullable_markers"
    )
    def nullable_markers(ctx: ActionContext, params: NullableMarkersParams) -> dict[str, Any]:
        received.append(params.room_id)
        return params.model_dump()

    params_classes["nullable_markers"] = NullableMarkersParams

    class DefaultRefParams(ActionParams):
        session_id: str = target(Session)
        room_id: str = ref(Room, default="r-b")

    @ontology.action(
        DefaultRefParams, target=Session, roles=["Coordinator"], api_name="default_ref"
    )
    def default_ref(ctx: ActionContext, params: DefaultRefParams) -> dict[str, Any]:
        return _book(ctx, params.room_id)

    params_classes["default_ref"] = DefaultRefParams

    class DefaultTargetParams(ActionParams):
        session_id: str = target(Session, required=False, default="s-b")
        room_id: str = ref(Room)

    @ontology.action(
        DefaultTargetParams, target=Session, roles=["Coordinator"], api_name="default_target"
    )
    def default_target(ctx: ActionContext, params: DefaultTargetParams) -> dict[str, Any]:
        return _book(ctx, params.room_id)

    params_classes["default_target"] = DefaultTargetParams

    class RequiredDefaultTargetParams(DefaultTargetParams):
        session_id: str = target(Session, default="s-b")

    ontology.action(
        RequiredDefaultTargetParams,
        target=Session,
        roles=["Coordinator"],
        api_name="required_default_target",
    )(default_target)

    params_classes["required_default_target"] = RequiredDefaultTargetParams

    def request_default() -> str:
        factory_calls.append("request-value")
        return factory_calls[-1]

    class AuditDefaultsParams(ActionParams):
        model_config = ConfigDict(populate_by_name=True)

        session_id: str = target(Session, required=False, default="s-a", alias="session")
        room_id: str = ref(Room, default="r-a")
        request_id: str = Field(default_factory=request_default, alias="request")

    @ontology.action(
        AuditDefaultsParams, target=Session, roles=["Coordinator"], api_name="audit_defaults"
    )
    def audit_defaults(ctx: ActionContext, params: AuditDefaultsParams) -> dict[str, Any]:
        received.append(params.room_id)
        return params.model_dump()

    params_classes["audit_defaults"] = AuditDefaultsParams
    ontology.validate()

    if backend == "in_memory":
        store: Store = InMemoryStore(ontology.registry)
    elif backend == "postgres":
        store = _postgres_store(ontology.registry, make_store)
    else:
        store = make_store(ontology.registry)

    source = Source(source_system="seed")
    for suffix in ("a", "b"):
        store.insert("Building", {"id": f"b-{suffix}"}, source)
        store.insert("Room", {"id": f"r-{suffix}"}, source)
        store.insert("Session", {"id": f"s-{suffix}"}, source)
        store.create_link("room_building", f"r-{suffix}", f"b-{suffix}")
        store.create_link("session_building", f"s-{suffix}", f"b-{suffix}")
    store.insert("Catalog", {"id": "catalog"}, source)
    consumer = make_consumer(
        actor_id="ca", role="Coordinator", scope_level="building", scope_id="b-a"
    )
    return _World(
        ontology, store, consumer, OntologyClient(ontology, store, consumer), params_classes,
        received, factory_calls,
    )


def _execute(
    world: _World, path: str, params: dict[str, Any], *, api_name: str = "book"
) -> dict[str, Any]:
    if path == "typed":
        return world.client.execute(world.params_classes[api_name](**params))
    if path == "dynamic":
        return world.client.execute(api_name, params)
    assert path == "mcp"
    server = build_mcp_server(world.ontology, world.store, world.consumer)
    result = asyncio.run(server.call_tool("execute_action", {"api_name": api_name, "params": params}))
    if result.structured_content is not None:
        return cast(dict[str, Any], result.structured_content)
    return cast(dict[str, Any], json.loads(result.content[0].text))


def _assert_denied(
    world: _World, path: str, params: dict[str, Any], *, api_name: str = "book"
) -> None:
    if path == "mcp":
        payload = _execute(world, path, params, api_name=api_name)
        assert payload["error"]["code"] == "SCOPE_DENIED"
    else:
        with raises_code(PermissionDenied, "SCOPE_DENIED"):
            _execute(world, path, params, api_name=api_name)


def _snapshot(world: _World) -> dict[str, Any]:
    return {
        "objects": {
            name: [row.model_dump() for row in world.store.read_all(name)]
            for name in world.ontology.registry.object_types
        },
        "links": {
            link: {obj_id: world.store.links_from(link, obj_id) for obj_id in ids}
            for link, ids in (
                ("room_building", ("r-a", "r-b")),
                ("session_building", ("s-a", "s-b")),
            )
        },
    }


@pytest.mark.parametrize("path", _PATHS)
def test_given_an_out_of_scope_ref_when_booking_then_the_engine_denies_without_writes(
    world: _World, path: str
) -> None:
    before = _snapshot(world)

    _assert_denied(world, path, {"session_id": "s-a", "room_id": "r-b"})

    assert world.received == []
    assert _snapshot(world) == before
    entries = world.store.audit_entries()
    assert len(entries) == 1
    assert entries[-1].outcome == "denied"
    assert entries[-1].error_code == "SCOPE_DENIED"
    assert entries[-1].writes == []


@pytest.mark.parametrize("path", _PATHS)
def test_given_an_in_scope_ref_when_booking_then_the_handler_succeeds(
    world: _World, path: str
) -> None:
    result = _execute(world, path, {"session_id": "s-a", "room_id": "r-a"})

    assert (result["result"] if path == "mcp" else result) == {"ok": "r-a"}
    assert world.received == ["r-a"]
    entry = world.store.audit_entries()[-1]
    assert entry.outcome == "ok"
    assert entry.error_code is None


@pytest.mark.parametrize("path", _PATHS)
def test_given_a_retired_out_of_scope_ref_when_booking_then_the_engine_denies(
    world: _World, path: str
) -> None:
    world.store.retire_object("Room", "r-b")
    assert world.store.read_current("Room", "r-b") is None
    assert world.store.read_last("Room", "r-b") is not None
    before = _snapshot(world)

    _assert_denied(world, path, {"session_id": "s-a", "room_id": "r-b"})

    assert world.received == []
    assert _snapshot(world) == before
    entry = world.store.audit_entries()[-1]
    assert entry.outcome == "denied"
    assert entry.error_code == "SCOPE_DENIED"
    assert entry.writes == []


def test_given_a_never_stored_ref_when_booking_then_the_handlers_precondition_refuses(
    world: _World,
) -> None:
    with raises_code(PreconditionFailed, "PRECONDITION_FAILED"):
        _execute(world, "dynamic", {"session_id": "s-a", "room_id": "r-missing"})

    assert world.received == ["r-missing"]
    entry = world.store.audit_entries()[-1]
    assert entry.outcome == "error"
    assert entry.error_code == "PRECONDITION_FAILED"


def test_given_a_retired_in_scope_ref_when_booking_then_the_handlers_precondition_refuses(
    world: _World,
) -> None:
    world.store.retire_object("Room", "r-a")

    with raises_code(PreconditionFailed, "PRECONDITION_FAILED"):
        _execute(world, "dynamic", {"session_id": "s-a", "room_id": "r-a"})

    assert world.received == ["r-a"]
    entry = world.store.audit_entries()[-1]
    assert entry.outcome == "error"
    assert entry.error_code == "PRECONDITION_FAILED"


@pytest.mark.parametrize("scope_id", ["b-a", "b-b", "b-missing"])
def test_given_an_unscoped_ref_when_an_in_role_consumer_calls_then_it_is_allowed_and_audited(
    world: _World, make_consumer: ConsumerFactory, scope_id: str
) -> None:
    consumer = make_consumer(role="Coordinator", scope_level="building", scope_id=scope_id)
    client = OntologyClient(world.ontology, world.store, consumer)

    assert client.execute("use_catalog", {"catalog_id": "catalog"}) == {"catalog_id": "catalog"}

    assert world.received == ["catalog"]
    entry = world.store.audit_entries()[-1]
    assert entry.outcome == "ok"
    assert entry.unscoped_params == ["catalog_id"]


@pytest.mark.parametrize("path", _PATHS)
def test_given_an_unscoped_ref_when_an_earlier_target_is_denied_then_the_ref_is_still_audited(
    world: _World, path: str
) -> None:
    _assert_denied(
        world, path, {"session_id": "s-b", "catalog_id": "catalog"}, api_name="use_session_catalog"
    )

    assert world.received == []
    entry = world.store.audit_entries()[-1]
    assert entry.outcome == "denied"
    assert entry.error_code == "SCOPE_DENIED"
    assert entry.unscoped_params == ["catalog_id"]


@pytest.mark.parametrize("declaration", ["optional"])
@pytest.mark.parametrize("path", ["typed", "dynamic"])
@pytest.mark.parametrize("explicit", [False, True], ids=["omitted", "explicit-none"])
def test_given_a_nullable_ref_when_omitted_or_none_then_the_handler_receives_none(
    world: _World, path: str, explicit: bool
) -> None:
    params: dict[str, Any] = {"session_id": "s-a"}
    if explicit:
        params["room_id"] = None

    assert _execute(world, path, params) == {"ok": None}

    assert world.received == [None]
    entry = world.store.audit_entries()[-1]
    assert entry.outcome == "ok"
    assert entry.error_code is None


@pytest.mark.parametrize("path", ["typed", "dynamic"])
@pytest.mark.parametrize("explicit", [False, True], ids=["omitted", "explicit-none"])
def test_given_nullable_target_scope_and_ref_when_none_then_the_gate_skips_all_three(
    world: _World, path: str, explicit: bool
) -> None:
    expected = {"session_id": None, "building_id": None, "room_id": None}

    assert _execute(world, path, expected if explicit else {}, api_name="nullable_markers") == expected

    assert world.received == [None]
    entry = world.store.audit_entries()[-1]
    assert entry.outcome == "ok"
    assert entry.error_code is None


@pytest.mark.parametrize("param_name", ["session_id", "building_id", "room_id"])
def test_given_a_non_string_reference_when_shape_validation_is_bypassed_then_scope_denies(
    world: _World, param_name: str
) -> None:
    executor = ActionExecutor(world.store, world.ontology.registry, world.ontology.definition.policy)
    action_def = world.ontology.registry.get_action_type("nullable_markers")

    with raises_code(PermissionDenied, "SCOPE_DENIED"):
        executor._enforce_scope(
            world.consumer,
            "nullable_markers",
            action_def,
            {param_name: 23},
            None,
            datetime.now(UTC),
        )

    assert world.received == []
    entry = world.store.audit_entries()[-1]
    assert entry.outcome == "denied"
    assert entry.error_code == "SCOPE_DENIED"


@pytest.mark.parametrize("declaration", ["description", "json_schema_extra", "optional"])
@pytest.mark.parametrize("path", _PATHS)
def test_given_a_ref_with_metadata_or_a_null_default_when_out_of_scope_then_there_is_no_opt_out(
    world: _World, path: str
) -> None:
    _assert_denied(world, path, {"session_id": "s-a", "room_id": "r-b"})

    assert world.received == []
    entry = world.store.audit_entries()[-1]
    assert entry.outcome == "denied"
    assert entry.error_code == "SCOPE_DENIED"


@pytest.mark.parametrize("path", ["dynamic", "mcp"])
def test_given_an_out_of_scope_default_ref_when_omitted_then_the_engine_denies(
    world: _World, path: str
) -> None:
    assert world.store.read_current("Room", "r-b") is not None
    assert world.store.links_from("room_building", "r-b") == ["b-b"]
    before = _snapshot(world)
    params = {"session_id": "s-a"}

    _assert_denied(world, path, params, api_name="default_ref")

    assert params == {"session_id": "s-a"}
    assert world.received == []
    assert _snapshot(world) == before
    entries = world.store.audit_entries()
    assert len(entries) == 1
    assert entries[0].outcome == "denied"
    assert entries[0].error_code == "SCOPE_DENIED"
    assert entries[0].params == {"session_id": "s-a", "room_id": "r-b"}
    assert entries[0].target_id == "s-a"
    assert entries[0].writes == []


@pytest.mark.parametrize("path", ["dynamic", "mcp"])
def test_given_an_out_of_scope_default_target_when_omitted_then_the_engine_denies(
    world: _World, path: str
) -> None:
    assert world.store.read_current("Session", "s-b") is not None
    assert world.store.links_from("session_building", "s-b") == ["b-b"]
    before = _snapshot(world)
    params = {"room_id": "r-a"}

    _assert_denied(world, path, params, api_name="default_target")

    assert params == {"room_id": "r-a"}
    assert world.received == []
    assert _snapshot(world) == before
    entries = world.store.audit_entries()
    assert len(entries) == 1
    assert entries[0].outcome == "denied"
    assert entries[0].error_code == "SCOPE_DENIED"
    assert entries[0].params == {"session_id": "s-b", "room_id": "r-a"}
    assert entries[0].target_id == "s-b"
    assert entries[0].writes == []


def test_given_a_default_target_left_required_when_omitted_then_params_are_invalid(
    world: _World,
) -> None:
    action_def = world.ontology.registry.get_action_type("required_default_target")
    declared = {param.name: param for param in action_def.parameters}
    assert declared["session_id"].required is True
    assert not world.params_classes["required_default_target"].model_fields["session_id"].is_required()
    assert world.store.read_current("Session", "s-b") is not None
    assert world.store.links_from("session_building", "s-b") == ["b-b"]
    before = _snapshot(world)
    params = {"room_id": "r-a"}

    with raises_code(ActionError, "INVALID_PARAMS"):
        _execute(world, "dynamic", params, api_name="required_default_target")

    assert params == {"room_id": "r-a"}
    assert world.received == []
    assert _snapshot(world) == before
    entries = world.store.audit_entries()
    assert len(entries) == 1
    assert entries[0].outcome == "error"
    assert entries[0].error_code == "INVALID_PARAMS"
    assert entries[0].params == params
    assert entries[0].target_id is None
    assert entries[0].writes == []


@pytest.mark.parametrize("path", ["dynamic", "mcp"])
def test_given_omitted_defaults_when_typed_and_dict_calls_then_audits_and_handlers_agree(
    world: _World, path: str
) -> None:
    assert world.store.read_current("Session", "s-a") is not None
    assert world.store.links_from("session_building", "s-a") == ["b-a"]
    assert world.store.read_current("Room", "r-a") is not None
    assert world.store.links_from("room_building", "r-a") == ["b-a"]
    assert world.factory_calls == []
    expected = {"session_id": "s-a", "room_id": "r-a", "request_id": "request-value"}

    typed_result = _execute(world, "typed", {}, api_name="audit_defaults")
    assert world.factory_calls == ["request-value"]
    params: dict[str, Any] = {}
    dict_result = _execute(world, path, params, api_name="audit_defaults")

    assert params == {}
    assert world.factory_calls == ["request-value", "request-value"]
    assert world.received == ["r-a", "r-a"]
    typed_entry, dict_entry = world.store.audit_entries()
    assert typed_entry.outcome == dict_entry.outcome == "ok"
    assert typed_entry.params == dict_entry.params == expected
    assert typed_entry.target_id == dict_entry.target_id == "s-a"
    assert typed_result == typed_entry.params
    assert (dict_result["result"] if path == "mcp" else dict_result) == dict_entry.params


@pytest.mark.parametrize("path", ["dynamic", "mcp"])
def test_given_explicit_params_when_defaults_exist_then_the_caller_values_are_preserved(
    world: _World, path: str
) -> None:
    params = {"session_id": "s-a", "room_id": "r-a", "request_id": "explicit"}
    before = params.copy()

    result = _execute(world, path, params, api_name="audit_defaults")

    assert (result["result"] if path == "mcp" else result) == before
    assert params == before
    assert world.factory_calls == []
    assert world.received == ["r-a"]
    entry = world.store.audit_entries()[-1]
    assert entry.params == before
    assert entry.outcome == "ok"
    assert entry.error_code is None
