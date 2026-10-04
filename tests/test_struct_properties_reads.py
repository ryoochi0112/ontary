"""Typed and string struct reads, query refusals, and stored-row diagnostics."""

from __future__ import annotations

import json
import os
import uuid
from datetime import date, datetime, timezone
from typing import Any, Literal, assert_type

import pytest
from conftest import raises_code
from pydantic import BaseModel

from ontary import Consumer, ObjectStore, Ontology, OntologyObject, Source, prop
from ontary.client import OntologyClient
from ontary.errors import ValidationFailed
from ontary.meta import ObjectTypeDef, PropertyDef, Sensitivity
from ontary.store import Store
from ontary.store.inmemory import InMemoryStore


class Money(BaseModel):
    value: float
    currency: str
    booked: date
    observed: datetime


MONEY = Money(
    value=100.0,
    currency="JPY",
    booked=date(2026, 9, 27),
    observed=datetime(2026, 9, 27, 12, 30, tzinfo=timezone.utc),
)
PLAIN = {
    "value": 100.0,
    "currency": "JPY",
    "booked": "2026-09-27",
    "observed": "2026-09-27T12:30:00+00:00",
}
SRC = Source(source_system="seed")

ontology = Ontology("struct-reads", scope_levels=["org"], min_n=1)


@ontology.object(layer="L0", scope="unscoped")
class Order(OntologyObject):
    id: str = prop(primary_key=True)
    amount: Money
    status: Literal["open", "closed"]
    metadata: dict[str, int]


@ontology.object(layer="L0", scope="unscoped")
class PrivateOrder(OntologyObject):
    id: str = prop(primary_key=True)
    amount: Money | None = prop(sensitivity=Sensitivity(human_visible=False))
    status: Literal["open", "closed"]


@pytest.fixture
def store() -> InMemoryStore:
    result = InMemoryStore(ontology.registry)
    result.insert(
        "Order",
        {"id": "o1", "amount": MONEY, "status": "open", "metadata": {"count": 1}},
        SRC,
    )
    result.insert(
        "Order",
        {"id": "o2", "amount": MONEY, "status": "closed", "metadata": {"count": 2}},
        SRC,
    )
    result.insert("PrivateOrder", {"id": "p1", "amount": MONEY, "status": "open"}, SRC)
    return result


def _client(store: Store) -> OntologyClient:
    consumer = Consumer(
        actor_id="reader", role="Clerk", scope_level="org", scope_id="org-1", kind="human"
    )
    return ontology.bind(store).for_consumer(consumer)


@pytest.mark.parametrize("backend", ["memory", "sqlite", "postgres"])
def test_typed_and_string_reads_round_trip_on_each_store(backend: str) -> None:
    if backend == "memory":
        store: Store = InMemoryStore(ontology.registry)
    elif backend == "sqlite":
        store = ObjectStore(ontology.registry)
    else:
        dsn = os.environ.get("ONTARY_TEST_POSTGRES_DSN")
        if dsn is None:
            pytest.skip("ONTARY_TEST_POSTGRES_DSN is not set")
        from ontary.store.postgres import PostgresStore

        store = PostgresStore(ontology.registry, dsn, tenant=f"struct-read-{uuid.uuid4().hex}")
    object_id = uuid.uuid4().hex
    store.insert(
        "Order",
        {"id": object_id, "amount": MONEY, "status": "open", "metadata": {"count": 1}},
        SRC,
    )
    client = _client(store)
    typed = client.get(Order, object_id)
    assert typed is not None and type(typed.amount) is Money
    assert typed.amount == MONEY
    plain = client.get("Order", object_id)
    assert plain is not None and type(plain.payload["amount"]) is dict
    assert plain.payload["amount"] == PLAIN


def test_typed_and_string_reads_keep_the_declared_struct_shape(store: InMemoryStore) -> None:
    client = _client(store)
    typed = client.get(Order, "o1")
    assert typed is not None
    assert_type(typed.amount, Money)
    assert type(typed.amount) is Money
    assert typed.amount == MONEY
    assert type(typed.amount.booked) is date
    assert type(typed.amount.observed) is datetime

    string = client.get("Order", "o1")
    assert string is not None
    assert type(string.payload["amount"]) is dict
    assert string.payload["amount"] == PLAIN
    assert set(string.payload["amount"]) == set(PLAIN)


def test_restricted_struct_is_redacted_whole(store: InMemoryStore) -> None:
    client = _client(store)
    string = client.get("PrivateOrder", "p1")
    assert string is not None
    assert "amount" not in string.payload
    assert string.payload["status"] == "open"

    typed = client.get(PrivateOrder, "p1")
    assert typed is not None
    assert typed.amount is None
    assert typed.redacted_fields == frozenset({"amount"})


@pytest.mark.parametrize(
    "condition",
    ["100 JPY", PLAIN, {"ne": PLAIN}, {"in": "wrong shape"}, {"bogus": PLAIN}],
    ids=["eq", "bare-dict", "ne", "in-before-operand", "unknown-before-operator"],
)
def test_where_refuses_struct_before_operand_parsing(
    store: InMemoryStore, condition: Any
) -> None:
    with raises_code(ValidationFailed, "OPERATOR_TYPE_MISMATCH") as excinfo:
        _client(store).list("Order", where={"amount": condition})
    assert "not supported on struct property 'amount'" in str(excinfo.value)


def test_order_by_refuses_struct_before_direction_check(store: InMemoryStore) -> None:
    with raises_code(ValidationFailed, "INVALID_PARAMS") as excinfo:
        _client(store).list("Order", order_by=("amount", "sideways"))  # type: ignore[arg-type]
    assert "not supported on struct property 'amount'" in str(excinfo.value)


def test_group_by_and_aggregate_keep_their_allow_list_codes(store: InMemoryStore) -> None:
    client = _client(store)
    with raises_code(ValidationFailed, "INVALID_GROUP_BY") as grouped:
        client.aggregate_by("Order", "metadata", "amount", func="count")
    assert "amount" in str(grouped.value)

    with raises_code(ValidationFailed, "NON_NUMERIC_AGGREGATE") as aggregated:
        client.aggregate("Order", "amount")
    assert "amount" in str(aggregated.value)


def test_non_struct_query_behaviour_is_unchanged(store: InMemoryStore) -> None:
    client = _client(store)
    assert [row.payload["id"] for row in client.list("Order", where={"status": "open"}, limit=None)] == ["o1"]
    assert [row.payload["id"] for row in client.list("Order", where={"metadata": {"ne": {"count": 1}}}, limit=None)] == ["o2"]
    assert [row.payload["id"] for row in client.list("Order", order_by="status", limit=None)] == ["o2", "o1"]


def test_validate_and_diagnose_report_bad_stored_struct(store: InMemoryStore) -> None:
    assert ontology.diagnose(store=store) == []
    ontology.validate(store=store)

    # Bypass write validation as an older store or damaged row can do.
    row = store._objects[0]
    payload = json.loads(row["payload"])
    payload["amount"]["booked"] = "20260927"
    row["payload"] = json.dumps(payload)

    with raises_code(ValidationFailed, "INVALID_RECORD") as hydrated:
        _client(store).get(Order, "o1")
    assert "booked: is not a valid ISO-8601 date" in str(hydrated.value)

    findings = ontology.diagnose(store=store)
    assert [(finding.code, finding.location) for finding in findings] == [
        ("INVALID_RECORD", "ObjectTypeDef['Order'].properties['amount']")
    ]
    assert "booked: is not a valid ISO-8601 date" in findings[0].message
    with raises_code(ValidationFailed, "INVALID_RECORD") as validated:
        ontology.validate(store=store)
    assert "amount" in str(validated.value)


def test_non_struct_diagnostics_still_report_its_property(store: InMemoryStore) -> None:
    row = store._objects[0]
    payload = json.loads(row["payload"])
    payload["status"] = 42
    row["payload"] = json.dumps(payload)
    findings = ontology.diagnose(store=store)
    assert [(finding.code, finding.location) for finding in findings] == [
        ("INVALID_RECORD", "ObjectTypeDef['Order'].properties['status']")
    ]


def test_diagnose_checks_a_struct_owned_default() -> None:
    local = Ontology("struct-owned-default", scope_levels=["org"])

    @local.object(
        layer="L0", scope="unscoped", owned={"amount": {**PLAIN, "booked": "20260927"}}
    )
    class OwnedOrder(OntologyObject):
        id: str = prop(primary_key=True)
        amount: Money

    findings = local.diagnose()
    assert [(finding.code, finding.location) for finding in findings] == [
        ("ONTOLOGY_INVALID", "ObjectTypeDef['OwnedOrder'].owned['amount']")
    ]
    assert "booked: is not a valid ISO-8601 date" in findings[0].message


def test_descriptor_only_diagnose_checks_stored_struct(store: InMemoryStore) -> None:
    local = Ontology("descriptor-only-struct", scope_levels=["org"])
    fields = next(
        prop.fields for prop in ontology.registry.get_object_type("Order").properties
        if prop.name == "amount"
    )
    local.registry.register_object_type(
        ObjectTypeDef(
            api_name="Order",
            display_name="Order",
            description="Descriptor-only order",
            layer="L0",
            properties=[
                PropertyDef(name="id", type="str"),
                PropertyDef(name="amount", type="struct", fields=fields),
                PropertyDef(name="status", type="str", choices=("open", "closed")),
                PropertyDef(name="metadata", type="json"),
            ],
            primary_key="id",
        )
    )
    assert local.diagnose(store=store) == []

    row = store._objects[0]
    payload = json.loads(row["payload"])
    payload["amount"]["value"] = "bad"
    row["payload"] = json.dumps(payload)

    findings = local.diagnose(store=store)
    assert [(finding.code, finding.location) for finding in findings] == [
        ("INVALID_RECORD", "ObjectTypeDef['Order'].properties['amount']")
    ]
    assert "1 of 2 current rows would fail hydration" in findings[0].message
    assert "value: expected type 'float', got str" in findings[0].message
