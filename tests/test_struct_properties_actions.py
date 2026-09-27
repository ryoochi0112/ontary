"""Struct action parameters and action-context writes."""

from __future__ import annotations

from datetime import date, datetime, timezone
from enum import Enum
from typing import Any, Literal, assert_type

import pytest
from conftest import raises_code
from pydantic import BaseModel

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
from ontary.client import OntologyClient
from ontary.errors import OntaryError


class Currency(Enum):
    JPY = "JPY"
    USD = "USD"


class Money(BaseModel):
    value: float
    currency: Currency
    booked: date
    observed: datetime


NOW = datetime(2026, 9, 27, 12, 30, tzinfo=timezone.utc)
MONEY = Money(value=100, currency=Currency.JPY, booked=date(2026, 9, 27), observed=NOW)
PLAIN = {
    "value": 100.0,
    "currency": "JPY",
    "booked": "2026-09-27",
    "observed": "2026-09-27T12:30:00+00:00",
}
SRC = Source(source_system="seed")
SEEN: list[Money] = []

ontology = Ontology("struct-actions", scope_levels=["org"], min_n=1)


@ontology.object(layer="L0", scope="unscoped", owned=True)
class Order(OntologyObject):
    id: str = prop(primary_key=True)
    amount: Money


class RepriceParams(ActionParams):
    order_id: str = target(Order)
    price: Money


class CreateParams(ActionParams):
    price: Money
    as_model: bool


class OtherParams(ActionParams):
    label: str
    kind: Literal["retail", "trade"]
    at: datetime
    metadata: dict[str, int]


@ontology.action(RepriceParams, target=Order, roles=["Clerk"], api_name="Reprice")
def _reprice(ctx: ActionContext, params: RepriceParams) -> dict[str, Any]:
    assert_type(params.price, Money)
    SEEN.append(params.price)
    order = ctx.get(Order, params.order_id)
    assert order is not None
    order.amount = params.price
    ctx.save(order)
    return {}


@ontology.action(CreateParams, target=Order, roles=["Clerk"], api_name="CreateOrder")
def _create(ctx: ActionContext, params: CreateParams) -> dict[str, Any]:
    amount: Money | dict[str, Any] = (
        params.price if params.as_model else params.price.model_dump()
    )
    order = ctx.create(Order, amount=amount)
    return {"order_id": order.id}


@ontology.action(OtherParams, target=Order, roles=["Clerk"], api_name="Other")
def _other(ctx: ActionContext, params: OtherParams) -> dict[str, Any]:
    return {"label": params.label, "kind": params.kind, "at": params.at.isoformat(),
            "metadata": params.metadata}


ontology.validate()


@pytest.fixture
def store() -> ObjectStore:
    result = ObjectStore(ontology.registry)
    result.insert("Order", {"id": "one", "amount": PLAIN}, SRC)
    return result


def _client(store: ObjectStore) -> OntologyClient:
    consumer = Consumer(
        actor_id="clerk", role="Clerk", scope_level="org", scope_id="org-1", kind="human"
    )
    return ontology.bind(store).for_consumer(consumer)


def test_dict_and_typed_action_params_deliver_money_and_save_whole_value(
    store: ObjectStore,
) -> None:
    client = _client(store)
    SEEN.clear()
    client.execute("Reprice", {"order_id": "one", "price": {
        "value": 200, "currency": Currency.USD,
        "booked": date(2026, 9, 28), "observed": NOW,
    }})
    assert len(SEEN) == 1 and type(SEEN[0]) is Money
    assert SEEN[0].currency is Currency.USD
    stored = store.read_current("Order", "one")
    assert stored is not None
    assert stored.payload["amount"] == {
        "value": 200.0, "currency": "USD", "booked": "2026-09-28",
        "observed": NOW.isoformat(),
    }

    client.execute(RepriceParams(order_id="one", price=MONEY))
    assert len(SEEN) == 2 and type(SEEN[1]) is Money
    assert SEEN[1] == MONEY
    stored = store.read_current("Order", "one")
    assert stored is not None and stored.payload["amount"] == PLAIN

    client.execute("Reprice", {"order_id": "one", "price": MONEY})
    assert len(SEEN) == 3 and type(SEEN[2]) is Money
    stored = store.read_current("Order", "one")
    assert stored is not None and stored.payload["amount"] == PLAIN


def test_omitted_optional_inner_action_field_reaches_handler_as_none() -> None:
    class OptionalMoney(BaseModel):
        value: float
        note: str | None

    local = Ontology("optional-action", scope_levels=["org"], min_n=1)

    @local.object(layer="L0", scope="unscoped", owned=True)
    class Item(OntologyObject):
        id: str = prop(primary_key=True)

    class Params(ActionParams):
        price: OptionalMoney

    seen: list[OptionalMoney] = []

    @local.action(Params, target=Item, roles=["Clerk"], api_name="Quote")
    def _quote(ctx: ActionContext, params: Params) -> dict[str, Any]:
        seen.append(params.price)
        return {}

    consumer = Consumer(actor_id="clerk", role="Clerk", scope_level="org", scope_id="org-1", kind="human")
    local.bind(ObjectStore(local.registry)).for_consumer(consumer).execute(
        "Quote", {"price": {"value": 1.0}}
    )
    assert seen == [OptionalMoney(value=1.0, note=None)]


def test_typed_and_dict_struct_params_refuse_naive_inner_datetime(store: ObjectStore) -> None:
    client = _client(store)
    SEEN.clear()
    naive = datetime(2026, 1, 1)
    with raises_code(OntaryError, "INVALID_PARAMS") as dict_error:
        client.execute("Reprice", {"order_id": "one", "price": {**PLAIN, "observed": naive}})
    with raises_code(OntaryError, "INVALID_PARAMS") as typed_error:
        client.execute(RepriceParams(order_id="one", price=Money(
            value=100, currency=Currency.JPY, booked=date(2026, 9, 27), observed=naive
        )))
    assert "price.observed: expected an offset-aware datetime" in str(dict_error.value)
    assert str(typed_error.value) == str(dict_error.value)
    assert SEEN == []


@pytest.mark.parametrize(
    ("bad", "message"),
    [
        ({"value": 100, "booked": PLAIN["booked"], "observed": PLAIN["observed"]},
         "price.currency: required field missing"),
        ({**PLAIN, "value": "bad"}, "price.value: expected type 'float', got str"),
        ({**PLAIN, "currency": "EUR"}, "price.currency: expected one of"),
        ({**PLAIN, "extra": 1}, "price.extra: unknown field"),
        ("100 JPY", "parameter 'price' expected type 'struct', got str"),
    ],
)
def test_dict_action_refuses_each_invalid_struct_and_accepts_valid_one(
    store: ObjectStore, bad: Any, message: str
) -> None:
    client = _client(store)
    SEEN.clear()
    with raises_code(OntaryError, "INVALID_PARAMS") as excinfo:
        client.execute("Reprice", {"order_id": "one", "price": bad})
    assert message in str(excinfo.value)
    assert SEEN == []
    unchanged = store.read_current("Order", "one")
    assert unchanged is not None and unchanged.payload["amount"] == PLAIN

    client.execute("Reprice", {"order_id": "one", "price": PLAIN})
    assert len(SEEN) == 1 and type(SEEN[0]) is Money


def test_ctx_create_with_model_and_dict_persists_same_struct(store: ObjectStore) -> None:
    client = _client(store)
    model_id = client.execute(CreateParams(price=MONEY, as_model=True))["order_id"]
    dict_id = client.execute("CreateOrder", {"price": PLAIN, "as_model": False})["order_id"]
    model = store.read_current("Order", model_id)
    plain = store.read_current("Order", dict_id)
    assert model is not None and plain is not None
    assert model.payload["amount"] == plain.payload["amount"] == PLAIN


def test_non_struct_action_parameters_keep_their_declared_rules(store: ObjectStore) -> None:
    class Meta(BaseModel):
        count: int

    client = _client(store)
    result = client.execute("Other", {
        "label": "ok", "kind": "retail", "at": NOW, "metadata": {"count": 1},
    })
    assert result == {
        "label": "ok", "kind": "retail", "at": NOW.isoformat(), "metadata": {"count": 1},
    }
    for key, bad, message in (
        ("label", Currency.JPY, "parameter 'label' expected type 'str', got Currency"),
        ("kind", "wholesale", "parameter 'kind' expected one of ['retail', 'trade'], got 'wholesale'"),
        ("at", "invalid", "parameter 'at' is not a valid ISO-8601 datetime: 'invalid'"),
        ("metadata", [1], "metadata\n  Input should be a valid dictionary"),
        ("metadata", Meta(count=1), "parameter 'metadata' expected type 'json', got Meta"),
    ):
        params: dict[str, Any] = {
            "label": "ok", "kind": "retail", "at": NOW.isoformat(),
            "metadata": {"count": 1},
        }
        params[key] = bad
        with raises_code(OntaryError, "INVALID_PARAMS") as excinfo:
            client.execute("Other", params)
        assert message in str(excinfo.value)
        if isinstance(bad, Meta):
            assert str(excinfo.value) == f"'Other': {message}"
