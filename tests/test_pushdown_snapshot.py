"""Full-stream reads judge one snapshot, even across a concurrent update."""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from functools import wraps

import pytest

from ontary.errors import VisibilityError
from ontary.meta import ObjectTypeDef, OntologyRegistry, PropertyDef
from ontary.query import GuardedQuery
from ontary.scope import DirectProperty, ScopePolicy
from ontary.security import Consumer
from ontary.store import DEFAULT_BATCH, ObjectStore, Source
from ontary.store._core import StoreCore

SRC = Source(source_system="snapshot-test")
ROW_COUNT = DEFAULT_BATCH + 200
CONSUMER = Consumer(actor_id="reader", role="Reader", scope_level="team",
                    scope_id="owned", kind="human")


@dataclass
class SnapshotStore:
    store: StoreCore
    writer: StoreCore
    registry: OntologyRegistry

    def query(self, *, scoped: bool = False, min_n: int = 5) -> GuardedQuery:
        policy = ScopePolicy(
            levels=["team"], min_n=min_n,
            rules={"Rows": [DirectProperty(level="team", property_name="owner")]} if scoped else {},
            unscoped_types=set() if scoped else {"Rows"},
            row_visibility={"Rows": lambda _s, _c, _t, payload: payload.get("vis") is True},
        )
        policy.validate(self.registry)
        return GuardedQuery(self.store, self.registry, policy)


@pytest.fixture(params=["sqlite", "postgres"])
def snapshot_store(request: pytest.FixtureRequest, tmp_path) -> Iterator[SnapshotStore]:
    registry = OntologyRegistry()
    registry.register_object_type(ObjectTypeDef(
        api_name="Rows", display_name="Rows", description="Snapshot regression", layer="L0",
        primary_key="id", properties=[
            PropertyDef(name="id", type="str"), PropertyDef(name="vis", type="bool"),
            PropertyDef(name="n", type="int"), PropertyDef(name="owner", type="str"),
        ],
    ))
    registry.validate()
    schema = None
    store = writer = None
    dsn = os.environ.get("ONTARY_TEST_POSTGRES_DSN")
    if request.param == "postgres":
        if not dsn:
            pytest.skip("ONTARY_TEST_POSTGRES_DSN is not set")
        import psycopg

        from ontary.store.postgres import PostgresStore

        schema = "ontary_test_snapshot_" + uuid.uuid4().hex
        with psycopg.connect(dsn, autocommit=True, options="-csearch_path=pg_catalog") as conn:
            conn.execute(f'CREATE SCHEMA "{schema}"')
    try:
        if schema is None:
            database = str(tmp_path / "snapshot.sqlite")
            store = ObjectStore(registry, database)
            writer = ObjectStore(registry, database)
        else:
            separator = "&" if "?" in dsn else "?"
            schema_dsn = f"{dsn}{separator}options=-csearch_path%3D{schema}"
            store = PostgresStore(registry, schema_dsn)
            writer = PostgresStore(registry, schema_dsn)
        with store.transaction():
            for i in range(ROW_COUNT):
                store.insert("Rows", {
                    "id": str(i), "vis": i < 4, "n": i,
                    "owner": "owned" if i < 4 else "foreign",
                }, SRC)
        yield SnapshotStore(store, writer, registry)
    finally:
        for opened in (store, writer):
            if opened is not None:
                opened._conn.close()
        if schema is not None:
            with psycopg.connect(dsn, autocommit=True, options="-csearch_path=pg_catalog") as conn:
                conn.execute(f'DROP SCHEMA "{schema}" CASCADE')


def _update_after_first_fetch(
    seeded: SnapshotStore, monkeypatch: pytest.MonkeyPatch, mutation: str,
) -> list[tuple[str, int]]:
    """Commit through a second connection after the first storage step returns.

    Hook the old page seam too, so these tests reproduce the failure before T8.
    The visibility swap is atomic: every snapshot still has exactly four visible
    objects, but deduplicating a batched walk would incorrectly keep five.
    """
    calls = []

    def hooked(name, original):
        @wraps(original)
        def fetch(*args, **kwargs):
            rows = original(*args, **kwargs)
            calls.append((name, len(rows)))
            if len(calls) == 1:
                with seeded.writer.transaction():
                    changes = {"n": 10000, "owner": "foreign"}
                    if mutation == "visibility_swap":
                        changes["vis"] = False
                    seeded.writer.update("Rows", "0", changes, SRC)
                    if mutation == "visibility_swap":
                        seeded.writer.update("Rows", str(ROW_COUNT - 1), {"vis": True}, SRC)
            return rows

        return fetch

    for name in ("_all_rows", "_filtered_all_rows", "_page_rows", "_filtered_page_rows"):
        if hasattr(seeded.store, name):
            monkeypatch.setattr(seeded.store, name, hooked(name, getattr(seeded.store, name)))
    return calls


@pytest.mark.parametrize("where", [None, {"n": {"gte": 0}}], ids=["plain", "filtered"])
@pytest.mark.parametrize("mutation", ["version", "visibility_swap"])
@pytest.mark.parametrize("path", ["aggregate", "count_contributors"])
def test_min_n_counts_one_snapshot(snapshot_store, monkeypatch, where, mutation, path) -> None:
    query = snapshot_store.query()
    calls = _update_after_first_fetch(snapshot_store, monkeypatch, mutation)
    with pytest.raises(VisibilityError) as refused:
        if path == "aggregate":
            query.aggregate(CONSUMER, "Rows", where=where, func="count")
        else:
            query.count_contributors(CONSUMER, "Rows", where)
    assert refused.value.code == "MIN_N_VIOLATION"
    assert calls == [("_all_rows" if where is None else "_filtered_all_rows", ROW_COUNT)]


@pytest.mark.parametrize("where", [None, {"n": {"gte": 0}}], ids=["plain", "filtered"])
@pytest.mark.parametrize("mutation", ["version", "visibility_swap"])
@pytest.mark.parametrize("order_by", [None, ("n", "desc")], ids=["row_order", "sorted"])
def test_unbounded_list_equals_one_snapshot(
    snapshot_store, monkeypatch, where, mutation, order_by,
) -> None:
    query = snapshot_store.query()
    expected = query.get_objects(CONSUMER, "Rows", where, limit=None, order_by=order_by)
    calls = _update_after_first_fetch(snapshot_store, monkeypatch, mutation)
    result = query.get_objects(CONSUMER, "Rows", where, limit=None, order_by=order_by)
    ids = [row.lineage.object_id for row in result]
    assert len(ids) == len(set(ids)), f"duplicate ids across storage calls: {ids}"
    assert result == expected
    assert calls == [("_all_rows" if where is None else "_filtered_all_rows", ROW_COUNT)]
    current = snapshot_store.writer.read_current("Rows", "0")
    assert current.payload["n"] == 10000  # The concurrent update really committed.


@pytest.mark.parametrize("where", [None, {"n": {"gte": 0}}], ids=["plain", "filtered"])
@pytest.mark.parametrize("mutation", ["version", "visibility_swap"])
def test_count_counts_one_snapshot(snapshot_store, monkeypatch, where, mutation) -> None:
    calls = _update_after_first_fetch(snapshot_store, monkeypatch, mutation)
    assert snapshot_store.query().count(CONSUMER, "Rows", where) == 4
    assert calls == [("_all_rows" if where is None else "_filtered_all_rows", ROW_COUNT)]


@pytest.mark.parametrize("path", ["unbounded", "count", "aggregate", "count_contributors"])
def test_full_stream_keeps_python_where_visibility_and_priming(
    snapshot_store, monkeypatch, path,
) -> None:
    """A permissive storage superset makes both Python gates essential."""
    calls = []

    def permissive(obj_type, row_filter):
        rows = snapshot_store.store._all_rows(obj_type)
        calls.append((row_filter, len(rows)))
        return rows

    def reread(*_args, **_kwargs):
        pytest.fail("scope resolution re-read a fetched row")

    monkeypatch.setattr(snapshot_store.store, "_filtered_all_rows", permissive, raising=False)
    monkeypatch.setattr(snapshot_store.store, "read_current", reread)
    query = snapshot_store.query(scoped=True, min_n=1)
    where = {"n": {"gte": 2}}
    if path == "unbounded":
        result = query.get_objects(CONSUMER, "Rows", where, limit=None)
        assert [row.lineage.object_id for row in result] == ["2", "3"]
    elif path == "aggregate":
        assert query.aggregate(CONSUMER, "Rows", where=where, func="count") == 2
    else:
        assert getattr(query, path)(CONSUMER, "Rows", where) == 2
    assert len(calls) == 1
    assert calls[0][1] == ROW_COUNT
