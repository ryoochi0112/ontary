"""The typed `ActionContext` surface (#41, PR 1 of 2).

A handler reads and writes through its ontology's own classes and link
handles instead of type-name strings and payload dicts:

- `ctx.get(Order, id) -> Order | None` and `ctx.all(Order) -> list[Order]`
- `ctx.create(Order, **values) -> Order`, filling a missing primary key from
  the runtime's `id_factory`
- `ctx.save(order)` writes only the fields changed since `get`/`create`
- `ctx.link` / `ctx.unlink` / `ctx.traverse` take a `LinkHandle[From, To]`
  and endpoints as `From | str` / `To | str`
- `ctx.retire(order)` or `ctx.retire(Order, id)`

This file is checked by `mypy --strict` (see the Makefile), so every
`assert_type` below is a static pin as well as a runtime one. The last test
runs mypy on a snippet to prove a wrong endpoint type or a misspelled field
is a static error.
"""

from __future__ import annotations

import itertools
import subprocess
import sys
import textwrap
from collections.abc import Callable
from pathlib import Path
from typing import Any, assert_type

import pytest
from conftest import raises_code

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
from ontary.client import OntologyClient, OntologyRuntime
from ontary.errors import ValidationFailed
from ontary.model import LinkHandle
from ontary.store import WriteRecord

ontology = Ontology("typed-ctx", scope_levels=["org"], min_n=1)


@ontology.object(layer="L0", scope="unscoped", owned=True)
class Customer(OntologyObject):
    id: str = prop(primary_key=True)
    name: str


@ontology.object(layer="L0", scope="unscoped", owned=True)
class Order(OntologyObject):
    id: str = prop(primary_key=True)
    status: str
    note: str | None = None


orderOf: LinkHandle[Order, Customer] = ontology.link(
    "orderOf", Order, Customer, "MANY_TO_ONE", owned=True
)

# What each handler observed, keyed by test; read back after `execute`.
SEEN: dict[str, Any] = {}


class OrderRef(ActionParams):
    order_id: str = target(Order)


class ReadOrderParams(OrderRef):
    pass


class ShipParams(OrderRef):
    pass


class SaveUnchangedParams(OrderRef):
    pass


class SaveForeignParams(OrderRef):
    pass


class ChangeKeyParams(OrderRef):
    pass


class TraverseParams(OrderRef):
    pass


class DetachParams(OrderRef):
    pass


class RetireObjParams(OrderRef):
    pass


class RetireByIdParams(OrderRef):
    pass


class PlaceParams(ActionParams):
    customer_id: str = target(Customer)
    status: str


class PlaceWithIdParams(ActionParams):
    customer_id: str = target(Customer)
    order_id: str


class PlaceUnknownFieldParams(ActionParams):
    customer_id: str = target(Customer)


@ontology.action(ReadOrderParams, target=Order, roles=["Clerk"], api_name="ReadOrder")
def _read_order(ctx: ActionContext, params: ReadOrderParams) -> dict[str, Any]:
    order = ctx.get(Order, params.order_id)
    assert_type(order, Order | None)
    missing = ctx.get(Order, "no-such-order")
    everything = ctx.all(Order)
    assert_type(everything, list[Order])
    SEEN["read"] = (order, missing, everything)
    return {}


@ontology.action(PlaceParams, target=Customer, roles=["Clerk"], api_name="Place")
def _place(ctx: ActionContext, params: PlaceParams) -> dict[str, Any]:
    order = ctx.create(Order, status=params.status)
    assert_type(order, Order)
    ctx.link(orderOf, order, params.customer_id)
    SEEN["place"] = order
    return {"order_id": order.id}


@ontology.action(PlaceWithIdParams, target=Customer, roles=["Clerk"], api_name="PlaceWithId")
def _place_with_id(ctx: ActionContext, params: PlaceWithIdParams) -> dict[str, Any]:
    order = ctx.create(Order, id=params.order_id, status="new")
    return {"order_id": order.id}


@ontology.action(
    PlaceUnknownFieldParams,
    target=Customer,
    roles=["Clerk"],
    api_name="PlaceUnknownField",
)
def _place_unknown(ctx: ActionContext, params: PlaceUnknownFieldParams) -> dict[str, Any]:
    ctx.create(Order, status="new", stauts="typo")
    return {}


@ontology.action(ShipParams, target=Order, roles=["Clerk"], api_name="Ship")
def _ship(ctx: ActionContext, params: ShipParams) -> dict[str, Any]:
    order = ctx.get(Order, params.order_id)
    assert order is not None
    order.status = "shipped"
    ctx.save(order)
    return {}


@ontology.action(SaveUnchangedParams, target=Order, roles=["Clerk"], api_name="SaveUnchanged")
def _save_unchanged(ctx: ActionContext, params: SaveUnchangedParams) -> dict[str, Any]:
    order = ctx.get(Order, params.order_id)
    assert order is not None
    ctx.save(order)
    return {}


@ontology.action(SaveForeignParams, target=Order, roles=["Clerk"], api_name="SaveForeign")
def _save_foreign(ctx: ActionContext, params: SaveForeignParams) -> dict[str, Any]:
    ctx.save(Order(id=params.order_id, status="forged"))
    return {}


@ontology.action(ChangeKeyParams, target=Order, roles=["Clerk"], api_name="ChangeKey")
def _change_key(ctx: ActionContext, params: ChangeKeyParams) -> dict[str, Any]:
    order = ctx.get(Order, params.order_id)
    assert order is not None
    order.id = "o-renamed"
    ctx.save(order)
    return {}


@ontology.action(TraverseParams, target=Order, roles=["Clerk"], api_name="Traverse")
def _traverse(ctx: ActionContext, params: TraverseParams) -> dict[str, Any]:
    customers = ctx.traverse(orderOf, params.order_id)
    assert_type(customers, list[Customer])
    orders = ctx.traverse(orderOf, customers[0], reverse=True)
    assert_type(orders, list[Order])
    SEEN["traverse"] = (customers, orders)
    return {}


@ontology.action(DetachParams, target=Order, roles=["Clerk"], api_name="Detach")
def _detach(ctx: ActionContext, params: DetachParams) -> dict[str, Any]:
    order = ctx.get(Order, params.order_id)
    assert order is not None
    ctx.unlink(orderOf, order, "c1")
    return {}


@ontology.action(RetireObjParams, target=Order, roles=["Clerk"], api_name="RetireObj")
def _retire_obj(ctx: ActionContext, params: RetireObjParams) -> dict[str, Any]:
    order = ctx.get(Order, params.order_id)
    assert order is not None
    ctx.retire(order)
    return {}


@ontology.action(RetireByIdParams, target=Order, roles=["Clerk"], api_name="RetireById")
def _retire_by_id(ctx: ActionContext, params: RetireByIdParams) -> dict[str, Any]:
    ctx.retire(Order, params.order_id)
    return {}


ontology.validate()

ConsumerFactory = Callable[..., Consumer]


@pytest.fixture(autouse=True)
def _clear_seen() -> None:
    SEEN.clear()


@pytest.fixture
def store() -> ObjectStore:
    store = ObjectStore(ontology.registry)
    src = Source(source_system="seed")
    store.insert("Customer", {"id": "c1", "name": "Ada"}, src)
    store.insert("Order", {"id": "o1", "status": "new"}, src)
    store.create_link("orderOf", "o1", "c1")
    return store


def _client(store: ObjectStore) -> OntologyClient:
    ids = (f"gen-{n}" for n in itertools.count(1))
    runtime = OntologyRuntime(ontology, store, id_factory=lambda: next(ids))
    return runtime.for_consumer(
        Consumer(
            actor_id="clerk-1",
            role="Clerk",
            scope_level="org",
            scope_id="org-1",
            kind="human",
        )
    )


# -- reads ---------------------------------------------------------------------


def test_get_and_all_return_typed_objects(store: ObjectStore) -> None:
    _client(store).execute("ReadOrder", {"order_id": "o1"})

    order, missing, everything = SEEN["read"]
    assert isinstance(order, Order)
    assert (order.id, order.status) == ("o1", "new")
    assert missing is None
    assert [o.id for o in everything] == ["o1"]


# -- create --------------------------------------------------------------------


def test_create_fills_the_primary_key_from_id_factory(store: ObjectStore) -> None:
    result = _client(store).execute("Place", {"customer_id": "c1", "status": "new"})

    order = SEEN["place"]
    assert isinstance(order, Order)
    assert result == {"order_id": "gen-2"}  # gen-1 is the invocation id
    current = store.read_current("Order", "gen-2")
    assert current is not None
    assert current.payload["status"] == "new"
    assert store.links_from("orderOf", "gen-2") == ["c1"]
    assert store.audit_entries()[-1].writes == [
        WriteRecord(op="create", object_type="Order", object_id="gen-2"),
        WriteRecord(op="link", link_type="orderOf", from_id="gen-2", to_id="c1"),
    ]


def test_create_keeps_an_explicit_primary_key(store: ObjectStore) -> None:
    result = _client(store).execute("PlaceWithId", {"customer_id": "c1", "order_id": "o-explicit"})

    assert result == {"order_id": "o-explicit"}
    assert store.read_current("Order", "o-explicit") is not None


def test_create_refuses_an_unknown_field(store: ObjectStore) -> None:
    with raises_code(ValidationFailed, "INVALID_RECORD") as exc_info:
        _client(store).execute("PlaceUnknownField", {"customer_id": "c1"})

    assert "stauts" in str(exc_info.value)
    assert [row.lineage.object_id for row in store.read_all("Order")] == ["o1"]


# -- save ----------------------------------------------------------------------


def test_save_writes_only_the_changed_fields(store: ObjectStore) -> None:
    _client(store).execute("Ship", {"order_id": "o1"})

    current = store.read_current("Order", "o1")
    assert current is not None
    assert current.payload["status"] == "shipped"
    assert store.audit_entries()[-1].writes == [
        WriteRecord(op="update", object_type="Order", object_id="o1"),
    ]


def test_save_without_changes_writes_nothing(store: ObjectStore) -> None:
    _client(store).execute("SaveUnchanged", {"order_id": "o1"})

    entry = store.audit_entries()[-1]
    assert entry.outcome == "ok"
    assert entry.writes == []


def test_save_refuses_an_object_the_context_did_not_load(store: ObjectStore) -> None:
    with raises_code(ValidationFailed, "OBJECT_NOT_LOADED"):
        _client(store).execute("SaveForeign", {"order_id": "o1"})

    current = store.read_current("Order", "o1")
    assert current is not None
    assert current.payload["status"] == "new"


def test_save_refuses_a_primary_key_change(store: ObjectStore) -> None:
    with raises_code(ValidationFailed, "PRIMARY_KEY_IMMUTABLE"):
        _client(store).execute("ChangeKey", {"order_id": "o1"})


# -- links ---------------------------------------------------------------------


def test_traverse_returns_typed_objects_both_ways(store: ObjectStore) -> None:
    _client(store).execute("Traverse", {"order_id": "o1"})

    customers, orders = SEEN["traverse"]
    assert [(type(c), c.id) for c in customers] == [(Customer, "c1")]
    assert [(type(o), o.id) for o in orders] == [(Order, "o1")]


def test_unlink_accepts_an_object_and_an_id(store: ObjectStore) -> None:
    _client(store).execute("Detach", {"order_id": "o1"})

    assert store.links_from("orderOf", "o1") == []


# -- retire --------------------------------------------------------------------


@pytest.mark.parametrize("action", ["RetireObj", "RetireById"])
def test_retire_takes_an_object_or_a_class_and_id(store: ObjectStore, action: str) -> None:
    _client(store).execute(action, {"order_id": "o1"})

    assert store.read_current("Order", "o1") is None
    assert store.links_from("orderOf", "o1") == []


# -- static checks ---------------------------------------------------------------


_BAD_SNIPPET = """
from typing import Any
from ontary import ActionContext
from tests.test_typed_action_context import Customer, Order, orderOf


def handler(ctx: ActionContext, customer: Customer, order: Order) -> None:
    ctx.link(orderOf, customer, order)
    fetched = ctx.get(Order, "o1")
    assert fetched is not None
    fetched.stauts = "shipped"
"""


def test_mypy_rejects_a_wrong_endpoint_and_a_misspelled_field(tmp_path: Path) -> None:
    snippet = tmp_path / "bad_handler.py"
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
    # Endpoint types are checked: a Customer is not an Order. mypy words it as
    # a failed inference of the handle's type parameters, on the `link` line.
    link_line = _BAD_SNIPPET.splitlines().index("    ctx.link(orderOf, customer, order)") + 1
    assert f"bad_handler.py:{link_line}: error: Cannot infer value of type parameter" in out
    assert 'of "link" of "ActionContext"' in out
    # Field names are checked on assignment.
    assert '"Order" has no attribute "stauts"' in out
    assert out.count(": error:") == 3, out
