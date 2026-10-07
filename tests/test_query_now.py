"""``query.now()`` (#154): a Function reads the runtime's injectable clock.

One instant per Function call, read on first use: the same bound clock as
``ctx.now()``, without declaring a capability (which would audit every call).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from docs_corpus import api_reference_text

from ontary import (
    BoundQuery,
    Consumer,
    InMemoryStore,
    Ontology,
    OntologyObject,
    Source,
    prop,
)
from ontary.client import OntologyRuntime
from ontary.testing import FixedClock, raises_code

T = datetime(2026, 1, 1, tzinfo=timezone.utc)

ontology = Ontology("query-now-154", scope_levels=["org"], min_n=1)


@ontology.object(layer="L0", scope="unscoped")
class WorkOrder(OntologyObject):
    id: str = prop(primary_key=True)
    due: datetime


@ontology.function(api_name="nowTwice")
def _now_twice(query: BoundQuery) -> list[datetime]:
    return [query.now(), query.now()]


@ontology.function(api_name="countOrders")
def _count_orders(query: BoundQuery) -> int:
    return query.count(WorkOrder)


@ontology.function(api_name="overdueIds")
def _overdue_ids(query: BoundQuery) -> list[str]:
    now = query.now()
    return sorted(order.id for order in query.list(WorkOrder, limit=None) if order.due < now)


CLERK = Consumer(
    actor_id="clerk-1", role="Clerk", scope_level="org", scope_id="org-1", kind="human"
)


class Ticking:
    """A clock that advances one second per read and counts its reads."""

    def __init__(self, start: datetime) -> None:
        self.calls = 0
        self._start = start

    def __call__(self) -> datetime:
        instant = self._start + timedelta(seconds=self.calls)
        self.calls += 1
        return instant


def test_query_now_returns_the_bound_clock_instant() -> None:
    runtime = ontology.bind(InMemoryStore(ontology.registry), clock=FixedClock(T))
    assert runtime.for_consumer(CLERK).call_function("nowTwice") == [
        T.isoformat(),
        T.isoformat(),
    ]


def test_query_now_is_one_instant_per_call_read_on_first_use() -> None:
    clock = Ticking(T)
    client = OntologyRuntime(ontology, InMemoryStore(ontology.registry), clock=clock).for_consumer(
        CLERK
    )

    before = clock.calls
    assert client.call_function("countOrders") == 0
    assert clock.calls == before  # a Function that never asks reads no clock

    first = client.call_function("nowTwice")
    second = client.call_function("nowTwice")
    assert first == [first[0]] * 2
    assert second == [second[0]] * 2
    # Function results are encoded to ISO strings at the JSON boundary (#167).
    assert datetime.fromisoformat(second[0]) == datetime.fromisoformat(first[0]) + timedelta(
        seconds=1
    )
    assert clock.calls == before + 2


def test_time_dependent_function_uses_query_now() -> None:
    store = InMemoryStore(ontology.registry)
    runtime = ontology.bind(store, clock=FixedClock(T))
    source = Source(source_system="test")
    store.insert("WorkOrder", {"id": "late", "due": T - timedelta(days=1)}, source)
    store.insert("WorkOrder", {"id": "soon", "due": T + timedelta(days=1)}, source)
    assert runtime.for_consumer(CLERK).call_function("overdueIds") == ["late"]


def test_naive_clock_is_refused_by_query_now() -> None:
    runtime = OntologyRuntime(
        ontology, InMemoryStore(ontology.registry), clock=lambda: datetime(2026, 1, 1)
    )
    with raises_code("CLOCK_NOT_TIMEZONE_AWARE"):
        runtime.for_consumer(CLERK).call_function("nowTwice")


def test_directly_built_bound_query_uses_the_default_clock() -> None:
    runtime = ontology.bind(InMemoryStore(ontology.registry))
    client = runtime.for_consumer(CLERK)
    query = BoundQuery(client._query, CLERK, ontology.registry)
    instant = query.now()
    assert instant.tzinfo is not None
    assert query.now() == instant


@pytest.mark.parametrize("name", ["api-actions-functions.md", "api-actions-functions.ja.md"])
def test_api_reference_documents_query_now(name: str) -> None:
    text = (Path(__file__).resolve().parent.parent / "docs" / name).read_text()
    section = text.split("### `BoundQuery`", 1)[1].split("\n### ", 1)[0]
    assert "`.now()`" in section
    assert "query.now()" in section
    lang = "ja" if name.endswith(".ja.md") else "en"
    capability_line = next(
        line for line in api_reference_text(lang).splitlines()
        if "ctx.now()" in line and (
            "not a capability" in line or "Capability ではなく" in line
        )
    )
    assert "query.now()" in capability_line
