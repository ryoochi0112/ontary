"""Compiler exactness implies SQL == the engine's Python judge on I1 seeds."""

from __future__ import annotations

import os
import random
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any

import pytest
from _fetch_counter import count_fallbacks

from ontary.meta import ObjectTypeDef, OntologyRegistry, PropertyDef
from ontary.query import GuardedQuery
from ontary.query._pushdown import build_row_filter
from ontary.query._where import _compile_where, _normalize_where
from ontary.scope import DirectProperty, ScopePolicy, SelfScope
from ontary.security import Consumer
from ontary.store import ObjectStore, Source
from ontary.store._core import StoreCore
from ontary.store._filter import RowFilter

N = 48
SRC = Source(source_system="exactness-test")
FIELDS = {
    "label": "str", "rank": "int", "score": "float", "active": "bool",
    "day": "date", "at": "datetime", "json_text": "json",
    "json_number": "json",
}
VALUES = {
    "label": ["", "open", "Open", "closed", "a%_\\b", "日本"],
    "rank": [-2**53, -7, 0, 1, N - 1, 2**53],
    "score": [-7.5, -0.25, 0.0, 0.25, 1.5, 12.75],
    "active": [False, True],
    "day": ["2025-12-31", "2026-01-01", "2026-01-02", "2026-02-01"],
    "at": ["2025-12-31T23:59:00", "2026-01-01T00:00:00",
           "2026-01-02T00:00:00", "2026-01-01T00:00:00+00:00",
           "2026-01-01T09:00:00+09:00", "2025-12-31T19:00:00-05:00",
           "2026-01-02T00:00:00+00:00"],
    "json_text": ["", "open", "Open", "a%_\\b", "日本"],
    "json_number": [-7, 0, 1, 1.5, 12.75],
}


@dataclass
class ExactStore:
    backend: str
    store: StoreCore
    registry: OntologyRegistry
    policy: ScopePolicy


def _registry() -> OntologyRegistry:
    registry = OntologyRegistry()
    registry.register_object_type(ObjectTypeDef(
        api_name="Item", display_name="Item", description="Well-formed I1 seed", layer="L0",
        primary_key="id", properties=[
            PropertyDef(name="id", type="str"), PropertyDef(name="team_id", type="str"),
            *(PropertyDef(name=name, type=kind, required=False) for name, kind in FIELDS.items()),
        ],
    ))
    registry.validate()
    return registry


def _seed(store: StoreCore) -> None:
    rng = random.Random(6803)
    with store.transaction():
        for i in range(N):
            payload = {name: rng.choice(values) for name, values in VALUES.items()}
            # Strict stored ISO instants: naive strings plus aware datetime
            # objects with mixed offsets (including equal-instant spellings).
            instant = datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(hours=i - 24)
            payload["at"] = (
                instant.replace(tzinfo=None).isoformat() if i % 4 == 0 else
                instant.astimezone(timezone(timedelta(hours=(0, 9, -5)[i % 3])))
            )
            payload["rank"] = i
            payload["day"] = date(2026, 1, 1) + timedelta(days=i % 4 - 1)
            # Optional values are null or absent, never wrong-typed. JSON
            # scalar fields retain a single kind, matching the tested operand.
            if i % 9 == 0:
                payload.pop("label")
                payload["score"] = None
            payload.update(id=str(i), team_id="owned" if i % 3 else "foreign")
            store.insert("Item", payload, SRC)
        # Numeric boundary values remain inside I1's exact integer domain.
        for i, rank in enumerate((-2**53, -7, 2**53)):
            store.insert("Item", {"id": f"boundary-{i}", "team_id": "owned", "rank": rank}, SRC)


@pytest.fixture(scope="module", params=["sqlite", "postgres"])
def exact_store(request: pytest.FixtureRequest) -> Iterator[ExactStore]:
    registry = _registry()
    policy = ScopePolicy(levels=["team"], min_n=1, rules={"Item": [
        DirectProperty(level="team", property_name="team_id"), SelfScope(level="team"),
    ]})
    policy.validate(registry)
    schema = None
    store = None
    dsn = os.environ.get("ONTARY_TEST_POSTGRES_DSN")
    if request.param == "postgres":
        if not dsn:
            pytest.skip("ONTARY_TEST_POSTGRES_DSN is not set")
        import psycopg

        from ontary.store.postgres import PostgresStore

        schema = "ontary_test_exact_" + uuid.uuid4().hex
        with psycopg.connect(dsn, autocommit=True, options="-csearch_path=pg_catalog") as conn:
            conn.execute(f'CREATE SCHEMA "{schema}"')
    try:
        if schema is None:
            store = ObjectStore(registry)
        else:
            separator = "&" if "?" in dsn else "?"
            store = PostgresStore(registry, f"{dsn}{separator}options=-csearch_path%3D{schema}")
        _seed(store)
        yield ExactStore(request.param, store, registry, policy)
    finally:
        if store is not None:
            store._conn.close()
        if schema is not None:
            with psycopg.connect(dsn, autocommit=True, options="-csearch_path=pg_catalog") as conn:
                conn.execute(f'DROP SCHEMA "{schema}" CASCADE')


def _prefilter_exact(store: StoreCore, row_filter: RowFilter) -> bool:
    """Single seam for the dispatcher to shim on the pre-T1 commit."""
    return store.prefilter_exact(row_filter)


def _assert_invariant(data: ExactStore, where: dict[str, Any], *,
                      scope_id: str = "owned", superset: bool = False) -> bool:
    consumer = Consumer(actor_id="reader", role="Reader", scope_level="team",
                        scope_id=scope_id, kind="human")
    matcher = _compile_where(data.registry, "Item", _normalize_where(where))
    assert matcher is not None
    row_filter = build_row_filter(data.registry, data.policy, consumer, "Item", matcher)
    assert row_filter is not None and row_filter.scope is not None
    assert not row_filter.scope.climb_possible
    query = GuardedQuery(data.store, data.registry, data.policy)
    expected = {row.lineage.object_id for row in data.store.read_all("Item")
                if matcher(row.payload) and query._visible(consumer, row)}
    exact = _prefilter_exact(data.store, row_filter)
    # A fallback must never turn this semantic comparison into a Python read.
    with count_fallbacks(data.store) as fallbacks:
        selected = {row.lineage.object_id
                    for row in data.store.read_all_filtered("Item", row_filter)}
    assert fallbacks.total() == 0, (data.backend, where, dict(fallbacks))
    detail = (data.backend, where, scope_id, selected - expected, expected - selected)
    if exact:
        assert selected == expected, detail
    if superset:
        assert expected <= selected, detail
    return exact


def _generated_filters() -> list[dict[str, Any]]:
    rng = random.Random(680030)
    filters = []
    for field, kind in FIELDS.items():
        operators = ["eq", "ne", "in"]
        if kind in {"int", "float", "date", "datetime"}:
            operators += ["gt", "gte", "lt", "lte", "and"]
        if kind == "str":
            operators += ["contains"]
        for op in operators:
            for _ in range(8):
                operand = rng.choice(VALUES[field])
                if op == "eq":
                    condition = operand if rng.randrange(4) else None
                elif op == "in":
                    size = rng.randrange(min(3, len(VALUES[field])) + 1)
                    condition = {"in": rng.sample(VALUES[field], size)}
                elif op == "and":
                    condition = {"gte": operand, "lt": rng.choice(VALUES[field])}
                elif op == "contains":
                    condition = {op: rng.choice(["%", "_", "\\", "o", "日", ""])}
                else:
                    condition = {op: operand}
                where = {field: condition}
                # Exercise top-level conjuncts as well as and children.
                if rng.randrange(3) == 0 and field != "active":
                    where["active"] = rng.choice([True, False])
                filters.append(where)
    # Enumeration rows 4/6/9/10: mixed exact/inexact and children, lenient
    # fromisoformat operands, and integers outside the PG operand domain.
    for _ in range(8):
        large = rng.choice([-2**60, 2**60])
        filters.extend([
            {"rank": {"gt": large}},
            {"rank": {"in": [large, rng.randrange(N)]}},
            {"rank": {"gt": large, "lt": rng.randrange(N)}},
            {"at": {"gt": rng.choice([
                "2026-01-01T23:59", "2026-01-01 23:59:00+00:00",
            ]), "lt": "2026-01-02T00:00:00+00:00"}},
        ])
    rng.shuffle(filters)
    return filters


def test_generated_exactness(exact_store: ExactStore, record_property) -> None:
    filters = _generated_filters()
    assert len(filters) >= 300
    exact_count = sum(_assert_invariant(exact_store, where,
                      scope_id="foreign" if i % 5 == 0 else "owned")
                      for i, where in enumerate(filters))
    rate = exact_count / len(filters)
    record_property("exact_rate", f"{exact_store.backend}: {exact_count}/{len(filters)} ({rate:.1%})")
    print(f"exact rate {exact_store.backend}: {exact_count}/{len(filters)} ({rate:.1%})")
    assert rate >= 0.60, (exact_store.backend, exact_count, len(filters))


@pytest.mark.parametrize("where", [
    pytest.param({"at": {"gt": "2026-01-01T23:59"}}, id="S1-no-seconds"),
    pytest.param({"at": {"gt": "2026-01-01 23:59:00+00:00"}}, id="S2-space-separator"),
    pytest.param({"rank": {"gt": 2**60}}, id="S3-large-gt"),
    pytest.param({"rank": {"in": [2**60, N - 1]}}, id="S4-large-in"),
    pytest.param({"rank": {"gt": 2**60, "lt": N}}, id="S5-mixed-and"),
    pytest.param({"rank": {"gt": N - 2}}, id="C0-control"),
])
def test_directed_exactness(exact_store: ExactStore, where: dict[str, Any]) -> None:
    _assert_invariant(exact_store, where, superset=exact_store.backend == "postgres")


@pytest.mark.parametrize("offset", [
    pytest.param("+16:00", id="offset-plus-16"),
    pytest.param("-16:00", id="offset-minus-16"),
    pytest.param("+23:59", id="offset-plus-23-59"),
    pytest.param("+15:59", id="offset-plus-15-59-control"),
])
def test_datetime_offset_exactness(exact_store: ExactStore, offset: str) -> None:
    _assert_invariant(exact_store, {"at": {"gt": "2026-01-01T00:00:00" + offset}},
                      superset=exact_store.backend == "postgres")
