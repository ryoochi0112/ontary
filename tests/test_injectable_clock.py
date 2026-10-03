"""Runtime clock behaviour (#46): one instant per invocation, ``ctx.now()``,
and the store adopting the bound clock.

Each test runs on the SQLite and in-memory backends. Postgres is covered by
the store conformance suite.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

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
    target,
)
from ontary.client import OntologyClient, OntologyRuntime
from ontary.ingest import bulk_upsert
from ontary.model import LinkHandle
from ontary.store import Store
from ontary.store._shared import iso_instant
from ontary.testing import FixedClock, SequentialIds, raises_code

T = datetime(2026, 1, 1, tzinfo=timezone.utc)

ontology = Ontology("clock-46", scope_levels=["org"], min_n=1)


@ontology.object(layer="L0", scope="unscoped")
class Customer(OntologyObject):
    id: str = prop(primary_key=True)
    name: str


@ontology.object(layer="L0", scope="unscoped", owned=True)
class Order(OntologyObject):
    id: str = prop(primary_key=True)
    status: str


orderOf: LinkHandle[Order, Customer] = ontology.link(
    "orderOf", Order, Customer, "MANY_TO_ONE", owned=True
)

NOW_CALLS: list[datetime] = []


class PlaceParams(ActionParams):
    customer_id: str = target(Customer)


class ShipParams(ActionParams):
    order_id: str = target(Order)


class ChurnParams(ActionParams):
    customer_id: str = target(Customer)
    update_id: str
    retire_id: str


@ontology.action(PlaceParams, target=Customer, roles=["Clerk"], api_name="Place")
def _place(ctx: ActionContext, params: PlaceParams) -> dict[str, Any]:
    order = ctx.create(Order, status="new")
    ctx.link(orderOf, order, params.customer_id)
    return {"order_id": order.id}


@ontology.action(ShipParams, target=Order, roles=["Clerk"], api_name="Ship")
def _ship(ctx: ActionContext, params: ShipParams) -> dict[str, Any]:
    NOW_CALLS.append(ctx.now())
    order = ctx.get(Order, params.order_id)
    assert order is not None
    order.status = "shipped"
    ctx.save(order)
    return {}


@ontology.action(ChurnParams, target=Customer, roles=["Clerk"], api_name="Churn")
def _churn(ctx: ActionContext, params: ChurnParams) -> dict[str, Any]:
    NOW_CALLS.append(ctx.now())
    order = ctx.create(Order, status="new")
    ctx.link(orderOf, order, params.customer_id)
    NOW_CALLS.append(ctx.now())
    other = ctx.get(Order, params.update_id)
    assert other is not None
    other.status = "shipped"
    ctx.save(other)
    NOW_CALLS.append(ctx.now())
    ctx.retire(Order, params.retire_id)  # cascades to its orderOf link
    NOW_CALLS.append(ctx.now())
    return {}


ontology.validate()

CLERK = Consumer(
    actor_id="clerk-1", role="Clerk", scope_level="org", scope_id="org-1", kind="human"
)
SRC = Source(source_system="seed")


class Ticking:
    """Each call returns one second later than the previous one."""

    def __init__(self, start: datetime) -> None:
        self.calls = 0
        self._start = start

    def __call__(self) -> datetime:
        value = self._start + timedelta(seconds=self.calls)
        self.calls += 1
        return value


class Settable:
    """A clock whose value the test moves (possibly to a naive datetime)."""

    def __init__(self, value: datetime) -> None:
        self.value = value

    def __call__(self) -> datetime:
        return self.value


@pytest.fixture(autouse=True)
def _clear() -> None:
    NOW_CALLS.clear()


@pytest.fixture(params=["sqlite", "in_memory"])
def store(request: pytest.FixtureRequest) -> Store:
    if request.param == "sqlite":
        return ObjectStore(ontology.registry)
    return InMemoryStore(ontology.registry)


def _stamps(store: Store) -> list[str]:
    """Every non-null valid_from / valid_to of every object and link row."""
    if isinstance(store, ObjectStore):
        out: list[str] = []
        for table in ("objects", "links"):
            for row in store._conn.execute(f"SELECT valid_from, valid_to FROM {table}"):
                out.extend(v for v in (row["valid_from"], row["valid_to"]) if v is not None)
        return out
    assert isinstance(store, InMemoryStore)
    return [
        v
        for row in [*store._objects, *store._links]
        for v in (row["valid_from"], row["valid_to"])
        if v is not None
    ]


def _client(rt: OntologyRuntime) -> OntologyClient:
    return rt.for_consumer(CLERK)


def _seed(store: Store) -> None:
    store.insert("Customer", {"id": "c1", "name": "Ada"}, SRC)
    store.insert("Order", {"id": "o1", "status": "new"}, SRC)
    store.insert("Order", {"id": "o2", "status": "new"}, SRC)
    store.create_link("orderOf", "o1", "c1")


def test_one_instant_for_ctx_writes_and_audit_with_ticking_clock(store: Store) -> None:
    clock = Ticking(T)
    rt = OntologyRuntime(ontology, store, clock=clock, id_factory=SequentialIds("id"))
    _seed(store)
    seed_stamps = _stamps(store)
    calls_before = clock.calls

    _client(rt).execute(
        "Churn", {"customer_id": "c1", "update_id": "o2", "retire_id": "o1"}
    )

    # The executor read the clock exactly once; nothing else in the action did.
    assert clock.calls == calls_before + 1
    instant = T + timedelta(seconds=calls_before)
    entry = store.audit_entries()[-1]
    assert entry.outcome == "ok"
    assert entry.ts == instant
    assert NOW_CALLS == [instant] * 4
    iso = iso_instant(instant)
    stamps = _stamps(store)
    # created order + its link (valid_from), updated o2 (old valid_to, new
    # valid_from), retired o1 and its cascaded link (valid_to).
    assert sorted(stamps) == sorted(seed_stamps + [iso] * 6)
    assert max(stamps) == iso
    assert store.read_current("Order", "o1") is None
    assert store.links_from("orderOf", "o1") == []
    o2 = store.read_current("Order", "o2")
    assert o2 is not None and o2.lineage.valid_from == iso


def test_canonical_snippet_everything_is_T_and_ids_are_sequential(store: Store) -> None:
    rt = OntologyRuntime(ontology, store, clock=FixedClock(T), id_factory=SequentialIds("id"))
    client = _client(rt)
    report = bulk_upsert(
        store, ontology.registry, "Customer", [{"id": "c1", "name": "Ada"}], SRC
    )
    assert report.errors == []
    assert report.inserted_ids == ["c1"]

    placed = client.execute("Place", {"customer_id": "c1"})
    client.execute("Ship", {"order_id": placed["order_id"]})

    assert placed == {"order_id": "id-2"}
    iso = iso_instant(T)
    stamps = _stamps(store)
    # customer from; order v1 from+to; order v2 from; link from.
    assert stamps == [iso] * len(stamps) and len(stamps) == 5
    entries = store.audit_entries()
    assert [(e.invocation_id, e.ts, e.outcome) for e in entries] == [
        ("id-1", T, "ok"),
        ("id-3", T, "ok"),
    ]
    order = store.read_current("Order", "id-2")
    assert order is not None
    assert (order.payload["status"], order.lineage.valid_from) == ("shipped", iso)


def test_bind_with_a_different_clock_conflicts_and_writes_nothing(store: Store) -> None:
    a, b = FixedClock(T), FixedClock(T + timedelta(days=1))
    OntologyRuntime(ontology, store, clock=a)

    with raises_code("CLOCK_CONFLICT"):
        OntologyRuntime(ontology, store, clock=b)

    assert store.audit_entries() == []
    assert _stamps(store) == []

    again = OntologyRuntime(ontology, store, clock=a, id_factory=SequentialIds("id"))
    assert again._clock is a
    bare = OntologyRuntime(ontology, store, id_factory=SequentialIds("id"))
    assert bare._clock is a
    store.insert("Customer", {"id": "c1", "name": "Ada"}, SRC)
    _client(bare).execute("Place", {"customer_id": "c1"})
    assert store.audit_entries()[-1].ts == T
    assert set(_stamps(store)) == {iso_instant(T)}


def test_clock_regression_rolls_back_and_audits_the_code(store: Store) -> None:
    clock = Settable(T)
    rt = OntologyRuntime(ontology, store, clock=clock, id_factory=SequentialIds("id"))
    store.insert("Order", {"id": "o1", "status": "new"}, SRC)
    earlier = T - timedelta(hours=1)
    clock.value = earlier
    before = _stamps(store)

    with raises_code("CLOCK_REGRESSION"):
        _client(rt).execute("Ship", {"order_id": "o1"})

    assert _stamps(store) == before
    current = store.read_current("Order", "o1")
    assert current is not None and current.payload["status"] == "new"
    entry = store.audit_entries()[-1]
    assert (entry.outcome, entry.error_code, entry.ts, entry.writes) == (
        "error",
        "CLOCK_REGRESSION",
        earlier,
        [],
    )


def test_naive_clock_is_refused_before_anything_is_stored(store: Store) -> None:
    clock = Settable(datetime(2026, 1, 1))
    rt = OntologyRuntime(ontology, store, clock=clock)

    with raises_code("CLOCK_NOT_TIMEZONE_AWARE"):
        _client(rt).execute("Place", {"customer_id": "c1"})

    assert store.audit_entries() == []
    assert _stamps(store) == []
    assert store.read_all("Order") == []


def test_default_clock_audit_ts_equals_written_valid_from(store: Store) -> None:
    rt = OntologyRuntime(ontology, store)
    store.insert("Customer", {"id": "c1", "name": "Ada"}, SRC)

    result = _client(rt).execute("Place", {"customer_id": "c1"})

    entry = store.audit_entries()[-1]
    order = store.read_current("Order", result["order_id"])
    assert order is not None
    assert order.lineage.valid_from is not None
    assert entry.ts.tzinfo is not None
    assert datetime.fromisoformat(order.lineage.valid_from) == entry.ts
    assert iso_instant(entry.ts) == order.lineage.valid_from
