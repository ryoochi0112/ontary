"""Descriptor-authored struct writes through SQLite store and ingest."""

from __future__ import annotations

import json
import os
import uuid
from datetime import date, datetime, timezone
from enum import Enum
from typing import Any

import pytest
from conftest import raises_code
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    computed_field,
    create_model,
    field_serializer,
    model_serializer,
)

from ontary import (
    ActionContext,
    ActionParams,
    Consumer,
    InMemoryStore,
    ObjectStore,
    Ontology,
    OntologyObject,
    Source,
    prop,
)
from ontary.client import OntologyRuntime
from ontary.errors import OntaryError, ValidationFailed
from ontary.ingest import bulk_upsert
from ontary.meta import ObjectTypeDef, OntologyRegistry, PropertyDef, StructFieldDef
from ontary.ontology import OntologyDef
from ontary.scope import ScopePolicy
from ontary.store import Store


class Currency(Enum):
    JPY = "JPY"
    USD = "USD"


class Money(BaseModel):
    value: float
    currency: Currency
    booked: date
    observed: datetime
    note: str | None = None


SRC = Source(source_system="seed")
FIELDS = (
    StructFieldDef(name="value", type="float"),
    StructFieldDef(name="currency", type="str", choices=("JPY", "USD")),
    StructFieldDef(name="booked", type="date"),
    StructFieldDef(name="observed", type="datetime"),
    StructFieldDef(name="note", type="str", required=False),
)
NOW = datetime(2026, 9, 27, 12, 30, tzinfo=timezone.utc)
MONEY = Money(value=100, currency=Currency.JPY, booked=date(2026, 9, 27), observed=NOW)
PLAIN = {
    "value": 100.0,
    "currency": "JPY",
    "booked": "2026-09-27",
    "observed": "2026-09-27T12:30:00+00:00",
    "note": None,
}


def _registry(*, owned: dict[str, Any] | None = None) -> OntologyRegistry:
    registry = OntologyRegistry()
    registry.register_object_type(
        ObjectTypeDef(
            api_name="Order",
            display_name="Order",
            description="An order",
            layer="L0",
            properties=[
                PropertyDef(name="id", type="str"),
                PropertyDef(name="amount", type="struct", fields=FIELDS),
                PropertyDef(name="status", type="str", choices=("open", "closed")),
            ],
            primary_key="id",
            owned=owned if owned is not None else False,
        )
    )
    return registry


@pytest.fixture
def store() -> ObjectStore:
    return ObjectStore(_registry())


def _client(store: ObjectStore, registry: OntologyRegistry) -> Any:
    definition = OntologyDef(
        "struct-write",
        registry,
        ScopePolicy(
            levels=["org"], unscoped_types={"Order"}, rules={},
            contributor_rules={}, row_visibility={}, min_n=1,
        ),
    )
    consumer = Consumer(
        actor_id="writer", role="Clerk", scope_level="org", scope_id="org-1", kind="human"
    )
    return OntologyRuntime(definition, store).for_consumer(consumer)


def test_model_and_dict_store_identical_payload_bytes(store: ObjectStore) -> None:
    store.insert("Order", {"id": "model", "amount": MONEY, "status": "open"}, SRC)
    store.insert("Order", {"id": "dict", "amount": PLAIN, "status": "open"}, SRC)
    model = store.read_current("Order", "model")
    plain = store.read_current("Order", "dict")
    assert model is not None and plain is not None
    assert json.dumps(model.payload["amount"]) == json.dumps(plain.payload["amount"])
    assert model.payload["amount"] == PLAIN


def test_omitted_optional_inner_field_is_stored_and_hydrated() -> None:
    class OptionalMoney(BaseModel):
        value: float
        note: str | None
        memo: str | None = None
        currency: str

    local = Ontology("optional-money", scope_levels=["org"], min_n=1)

    @local.object(layer="L0", scope="unscoped")
    class Order(OntologyObject):
        id: str = prop(primary_key=True)
        amount: OptionalMoney

    amount_def = next(p for p in local.registry.get_object_type("Order").properties if p.name == "amount")
    assert amount_def.fields is not None
    assert [(field.name, field.required) for field in amount_def.fields] == [
        ("value", True), ("note", False), ("memo", False), ("currency", True),
    ]
    store = ObjectStore(local.registry)
    store.insert("Order", {"id": "dict", "amount": {"value": 1.0, "currency": "USD"}}, SRC)
    store.insert("Order", {"id": "model", "amount": OptionalMoney(
        value=1.0, note=None, currency="USD"
    )}, SRC)
    raw = store.read_current("Order", "dict")
    model = store.read_current("Order", "model")
    assert raw is not None and model is not None
    assert raw.payload["amount"] == model.payload["amount"] == {
        "value": 1.0, "note": None, "memo": None, "currency": "USD",
    }
    client = local.bind(store).for_consumer(Consumer(
        actor_id="reader", role="Clerk", scope_level="org", scope_id="org-1", kind="human"
    ))
    hydrated = client.get(Order, "dict")
    assert hydrated is not None
    assert hydrated.amount == OptionalMoney(value=1.0, note=None, memo=None, currency="USD")


@pytest.mark.parametrize(
    "bad,path",
    [
        ({"value": 100, "booked": "2026-09-27", "observed": NOW.isoformat()}, "currency"),
        ({**PLAIN, "value": "bad"}, "value"),
        ({**PLAIN, "currency": "EUR"}, "currency"),
        ({**PLAIN, "extra": 1}, "extra"),
        ("100 JPY", "amount"),
    ],
)
def test_invalid_struct_refused_on_each_write_entry(
    store: ObjectStore, bad: Any, path: str
) -> None:
    registry = store._registry
    expected = f"amount.{path}" if path != "amount" else "property 'amount'"
    store.insert("Order", {"id": "existing", "amount": PLAIN, "status": "open"}, SRC)
    record = {"id": "bad", "amount": bad, "status": "open"}

    with raises_code(ValidationFailed, "INVALID_RECORD") as inserted:
        store.insert("Order", record, SRC)
    assert expected in str(inserted.value)
    assert store.read_current("Order", "bad") is None

    with raises_code(ValidationFailed, "INVALID_RECORD") as updated:
        store.update("Order", "existing", {"amount": bad}, SRC)
    assert expected in str(updated.value)
    existing = store.read_current("Order", "existing")
    assert existing is not None and existing.payload["amount"] == PLAIN

    report = bulk_upsert(store, registry, "Order", [record], SRC)
    assert not report.ok and report.errors[0].code == "INVALID_RECORD"
    assert expected in (report.errors[0].reason or "")

    client_report = _client(store, registry).ingest("Order", [record], SRC, on_error="report")
    assert not client_report.ok and client_report.errors[0].code == "INVALID_RECORD"
    assert expected in (client_report.errors[0].reason or "")


def test_update_replaces_whole_struct(store: ObjectStore) -> None:
    store.insert("Order", {"id": "one", "amount": {**PLAIN, "note": "old"}, "status": "open"}, SRC)
    replacement = {key: value for key, value in PLAIN.items() if key != "note"}
    replacement["currency"] = Currency.USD
    store.update("Order", "one", {"amount": replacement}, SRC)
    stored = store.read_current("Order", "one")
    assert stored is not None
    assert stored.payload["amount"] == {**replacement, "currency": "USD", "note": None}


def test_owned_struct_default_is_normalized_and_validated() -> None:
    registry = _registry(owned={"amount": MONEY})
    registry.validate()
    obj_def = registry.get_object_type("Order")
    assert obj_def.owned_property_defaults() == {"amount": MONEY.model_dump() | {"currency": "JPY"}}
    store = ObjectStore(registry)
    report = bulk_upsert(store, registry, "Order", [{"id": "one", "status": "open"}], SRC)
    assert report.ok
    stored = store.read_current("Order", "one")
    assert stored is not None and stored.payload["amount"] == PLAIN

    invalid = _registry(owned={"amount": {**PLAIN, "currency": "EUR"}})
    with raises_code(ValidationFailed, "ONTOLOGY_INVALID") as excinfo:
        invalid.validate()
    assert "amount.currency" in str(excinfo.value)


def test_non_struct_choice_does_not_unwrap_without_declaration(store: ObjectStore) -> None:
    with raises_code(ValidationFailed, "INVALID_RECORD"):
        store.insert("Order", {"id": Currency.JPY, "amount": PLAIN, "status": "open"}, SRC)


class ExcludedOptional(BaseModel):
    a: int
    b: int | None = Field(default=None, exclude=True)


class ExcludedRequired(BaseModel):
    a: int = Field(exclude=True)


class SerializedField(BaseModel):
    a: int
    b: str

    @field_serializer("b")
    def serialize_b(self, value: str) -> str:
        return value.upper()


class SerializedModel(BaseModel):
    a: int
    b: str

    @model_serializer(mode="plain")
    def serialize_model(self) -> dict[str, str]:
        return {"different": self.b}


class ComputedValue(BaseModel):
    a: int

    @computed_field  # type: ignore[prop-decorator]
    @property
    def calculated(self) -> int:
        return self.a + 1


@pytest.fixture(params=["mem", "sqlite", "pg"])
def conformance_backend(request: pytest.FixtureRequest) -> str:
    backend: str = request.param
    if backend == "pg" and not os.getenv("ONTARY_TEST_POSTGRES_DSN"):
        pytest.skip("ONTARY_TEST_POSTGRES_DSN is not set")
    return backend


@pytest.mark.parametrize(
    "model,plain",
    [
        (ExcludedOptional(a=1, b=5), {"a": 1, "b": 5}),
        (ExcludedRequired(a=5), {"a": 5}),
        (SerializedField(a=1, b="raw"), {"a": 1, "b": "raw"}),
        (SerializedModel(a=1, b="raw"), {"a": 1, "b": "raw"}),
        (ComputedValue(a=1), {"a": 1}),
    ],
)
def test_serializer_shapes_store_identically_on_each_backend(
    conformance_backend: str, model: BaseModel, plain: dict[str, Any]
) -> None:
    local = Ontology("serializer-shapes", scope_levels=["org"], min_n=1)

    entry_cls = create_model(
        "Entry", __base__=OntologyObject,
        id=(str, prop(primary_key=True)), detail=(type(model), ...),
    )
    local.object(layer="L0", scope="unscoped", owned=True)(entry_cls)

    backend: Store
    if conformance_backend == "mem":
        backend = InMemoryStore(local.registry)
    elif conformance_backend == "sqlite":
        backend = ObjectStore(local.registry)
    else:
        from ontary.store import PostgresStore

        backend = PostgresStore(
            local.registry, os.environ["ONTARY_TEST_POSTGRES_DSN"],
            tenant=f"serializer-{uuid.uuid4().hex}",
        )
    backend.insert("Entry", {"id": "instance", "detail": model}, SRC)
    backend.insert("Entry", {"id": "dict", "detail": plain}, SRC)
    instance_row = backend.read_current("Entry", "instance")
    dict_row = backend.read_current("Entry", "dict")
    assert instance_row is not None and dict_row is not None
    assert instance_row.payload["detail"] == dict_row.payload["detail"] == plain


def test_model_extra_is_refused_as_unknown_inner_field() -> None:
    class WithExtra(BaseModel):
        model_config = ConfigDict(extra="allow")
        a: int = Field(exclude=True)

    local = Ontology("extra-struct", scope_levels=["org"], min_n=1)

    @local.object(layer="L0", scope="unscoped", owned=True)
    class Entry(OntologyObject):
        id: str = prop(primary_key=True)
        detail: WithExtra

    backend = ObjectStore(local.registry)
    with raises_code(ValidationFailed, "INVALID_RECORD") as excinfo:
        backend.insert(
            "Entry", {"id": "one", "detail": WithExtra.model_validate({"a": 1, "extra": 7})}, SRC
        )
    assert "property 'detail.extra' unknown field" in str(excinfo.value)
    backend.insert("Entry", {"id": "two", "detail": WithExtra(a=1)}, SRC)
    row = backend.read_current("Entry", "two")
    assert row is not None and row.payload["detail"] == {"a": 1}


@pytest.mark.parametrize("entry", ["insert", "action"])
def test_subclass_field_is_refused_on_insert_and_typed_action_param(
    conformance_backend: str, entry: str,
) -> None:
    class Detail(BaseModel):
        a: int

    class SubDetail(Detail):
        zzz: int

    local = Ontology("subclass-field", scope_levels=["org"], min_n=1)

    @local.object(layer="L0", scope="unscoped", owned=True)
    class Entry(OntologyObject):
        id: str = prop(primary_key=True)
        detail: Detail

    class Params(ActionParams):
        detail: Detail

    seen: list[Detail] = []

    @local.action(Params, target=Entry, roles=["Clerk"], api_name="UseDetail")
    def use_detail(ctx: ActionContext, params: Params) -> dict[str, Any]:
        seen.append(params.detail)
        return {}

    backend: Store
    if conformance_backend == "mem":
        backend = InMemoryStore(local.registry)
    elif conformance_backend == "sqlite":
        backend = ObjectStore(local.registry)
    else:
        from ontary.store import PostgresStore

        backend = PostgresStore(
            local.registry, os.environ["ONTARY_TEST_POSTGRES_DSN"],
            tenant=f"subclass-{uuid.uuid4().hex}",
        )

    if entry == "insert":
        for row_id, detail in (
            ("instance", SubDetail(a=1, zzz=2)),
            ("dict", {"a": 1, "zzz": 2}),
        ):
            with raises_code(ValidationFailed, "INVALID_RECORD") as excinfo:
                backend.insert("Entry", {"id": row_id, "detail": detail}, SRC)
            assert "property 'detail.zzz' unknown field" in str(excinfo.value)
            assert backend.read_current("Entry", row_id) is None
        return

    consumer = Consumer(
        actor_id="writer", role="Clerk", scope_level="org", scope_id="org-1", kind="human"
    )
    client = local.bind(backend).for_consumer(consumer)
    with raises_code(OntaryError, "INVALID_PARAMS") as typed_error:
        client.execute(Params(detail=SubDetail(a=1, zzz=2)))
    assert "detail.zzz: unknown field" in str(typed_error.value)
    with raises_code(OntaryError, "INVALID_PARAMS") as dict_error:
        client.execute("UseDetail", {"detail": {"a": 1, "zzz": 2}})
    assert "detail.zzz: unknown field" in str(dict_error.value)
    assert seen == []
