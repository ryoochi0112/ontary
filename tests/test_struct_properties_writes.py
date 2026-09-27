"""Descriptor-authored struct writes through SQLite store and ingest."""

from __future__ import annotations

import json
from datetime import date, datetime, timezone
from enum import Enum
from typing import Any

import pytest
from conftest import raises_code
from pydantic import BaseModel

from ontary import Consumer, ObjectStore, Source
from ontary.client import OntologyRuntime
from ontary.errors import ValidationFailed
from ontary.ingest import bulk_upsert
from ontary.meta import ObjectTypeDef, OntologyRegistry, PropertyDef, StructFieldDef
from ontary.ontology import OntologyDef
from ontary.scope import ScopePolicy


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
    assert stored.payload["amount"] == {**replacement, "currency": "USD"}
    assert "note" not in stored.payload["amount"]


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
