"""Batch reads preserve single-read semantics on every shipped backend."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from enum import Enum
from unittest.mock import Mock

import pytest
from conftest import raises_code
from test_store_conformance import STORE_FACTORIES, StoreFactory, build_registry

from ontary.errors import ValidationFailed
from ontary.meta import Cardinality, LinkTypeDef
from ontary.store import InMemoryStore, ObjectStore, Source, Store, _sql
from ontary.store._core import StoreCore
from ontary.store.postgres import PostgresStore


@pytest.fixture(params=STORE_FACTORIES, ids=lambda f: f.__name__.removeprefix("_make_"))
def store_factory(request: pytest.FixtureRequest) -> StoreFactory:
    return request.param


class _Id(str, Enum):
    MANY = "many"
    TARGET = "z"


def _registry():
    registry = build_registry()
    registry.register_link_type(
        LinkTypeDef(
            api_name="batchLink",
            from_type="Team",
            to_type="Department",
            cardinality=Cardinality.MANY_TO_MANY,
            description="Batch-read graph",
        )
    )
    registry.validate()
    return registry


def _seed(store: Store) -> None:
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    store.bind_clock(lambda: now)
    source = Source(source_system="batch-tests")
    for obj_type, ids in (
        ("Team", ["zero", "one", "many", "retired"]),
        ("Department", ["zero", "a", "z", "é", "closed"]),
    ):
        for obj_id in ids:
            store.insert(obj_type, {"id": obj_id, "name": obj_id}, source)
    for from_id, to_id in (
        ("many", "é"),
        ("many", "z"),
        ("many", "a"),
        ("one", "z"),
        ("many", "closed"),
    ):
        store.create_link("batchLink", from_id, to_id)
    store.close_link("batchLink", "many", "closed")
    store.retire_object("Team", "retired")
    store.update("Team", "one", {"name": "updated"}, source)
    now = datetime(2026, 1, 2, tzinfo=timezone.utc)
    store.create_link("batchLink", "many", "zero")


def test_batch_reads_equal_single_reads(store_factory: StoreFactory) -> None:
    store = store_factory(_registry())
    _seed(store)
    anchors = ["many", "missing", "zero", "one", "retired"]
    expected = {
        obj_id: row for obj_id in anchors if (row := store.read_current("Team", obj_id)) is not None
    }
    rows = store.read_current_many("Team", iter(anchors))
    assert rows == expected
    assert list(rows) == ["many", "zero", "one"]
    assert "retired" not in rows
    forward = store.links_from_many("batchLink", iter(anchors))
    assert forward == {obj_id: store.links_from("batchLink", obj_id) for obj_id in anchors}
    assert list(forward) == anchors
    assert forward["many"] == ["a", "z", "é", "zero"]
    targets = ["z", "missing", "zero", "a", "é", "closed"]
    reverse = store.links_to_many("batchLink", iter(targets))
    assert reverse == {obj_id: store.links_to("batchLink", obj_id) for obj_id in targets}
    assert list(reverse) == targets
    assert reverse["z"] == ["many", "one"]
    assert reverse["closed"] == []


def test_batch_ids_are_canonical_and_deduplicated(store_factory: StoreFactory) -> None:
    store = store_factory(_registry())
    _seed(store)
    ids = [_Id.MANY, "one", "many", "one", "missing"]
    rows = store.read_current_many("Team", iter(ids))
    assert list(rows) == ["many", "one"]
    assert rows == store.read_current_many("Team", ["many", "one"])
    forward = store.links_from_many("batchLink", iter(ids))
    assert list(forward) == ["many", "one", "missing"]
    assert forward == store.links_from_many("batchLink", ["many", "one", "missing"])
    reverse = store.links_to_many("batchLink", [_Id.TARGET, "a", "z"])
    assert list(reverse) == ["z", "a"]
    assert reverse == store.links_to_many("batchLink", ["z", "a"])
    assert all(type(key) is str for key in [*rows, *forward, *reverse])


@pytest.mark.parametrize(
    ("method", "step", "type_name"),
    [
        ("read_current_many", "_current_rows_many", "Team"),
        ("links_from_many", "_link_ids_from_many", "batchLink"),
        ("links_to_many", "_link_ids_to_many", "batchLink"),
    ],
)
def test_empty_batch_calls_no_storage_step(
    store_factory: StoreFactory, monkeypatch, method, step, type_name
) -> None:
    store = store_factory(_registry())
    storage = Mock(side_effect=AssertionError("empty batch reached storage"))
    monkeypatch.setattr(store, step, storage)
    assert getattr(store, method)(type_name, iter([])) == {}
    storage.assert_not_called()


@pytest.mark.parametrize("method", ["links_from_many", "links_to_many"])
@pytest.mark.parametrize("ids", [[], ["missing"]])
def test_unknown_link_type_is_coded(store_factory: StoreFactory, method, ids) -> None:
    store = store_factory(_registry())
    with raises_code(ValidationFailed, "UNKNOWN_LINK_TYPE"):
        getattr(store, method)("undeclared", iter(ids))


def test_batch_reads_isolate_tenants(store_factory: StoreFactory, tmp_path) -> None:
    registry = _registry()
    store = store_factory(registry)
    if isinstance(store, ObjectStore):
        store._conn.close()
        path = str(tmp_path / "batch-tenants.db")
        store = ObjectStore(registry, path)
        other = ObjectStore(registry, path, tenant="other")
    elif isinstance(store, PostgresStore):
        other = PostgresStore(registry, store._conn.info.dsn, tenant="other")
    else:
        other = InMemoryStore(registry, tenant="other")
    _seed(store)
    _seed(other)
    source = Source(source_system="other-tenant")
    other.insert("Team", {"id": "foreign", "name": "foreign"}, source)
    other.insert("Department", {"id": "foreign", "name": "foreign"}, source)
    other.update("Team", "many", {"name": "other"}, source)
    other.create_link("batchLink", "many", "foreign")
    other.create_link("batchLink", "foreign", "z")
    assert store.read_current_many("Team", ["many", "foreign"]) == {
        "many": store.read_current("Team", "many")
    }
    assert store.links_from_many("batchLink", ["many", "foreign"]) == {
        "many": ["a", "z", "é", "zero"],
        "foreign": [],
    }
    assert store.links_to_many("batchLink", ["z", "foreign"]) == {
        "z": ["many", "one"],
        "foreign": [],
    }


def test_core_defaults_loop_single_storage_steps() -> None:
    store = InMemoryStore(_registry())
    _seed(store)
    for name, single, type_name, ids in (
        ("_current_rows_many", "read_current", "Team", ["many", "missing", "retired"]),
        ("_link_ids_from_many", "links_from", "batchLink", ["many", "zero", "missing"]),
        ("_link_ids_to_many", "links_to", "batchLink", ["z", "zero", "missing"]),
    ):
        expected = {obj_id: getattr(store, single)(type_name, obj_id) for obj_id in ids}
        if name == "_current_rows_many":
            expected = {key: value for key, value in expected.items() if value is not None}
        assert getattr(StoreCore, name)(store, type_name, ids) == expected


def test_batch_preserves_duplicate_rows_and_first_live_pick(store_factory: StoreFactory) -> None:
    store = store_factory(_registry())
    if isinstance(store, (ObjectStore, PostgresStore)):
        # Exercise the defensive pick/grouping rules in a disposable store.
        store._conn.execute("DROP INDEX idx_objects_live_id")
        store._conn.execute("DROP INDEX idx_links_live_pair")
        store._conn.commit()
    _seed(store)
    source = Source(source_system="duplicate-row")
    store._insert_object_row(
        "Team",
        "many",
        json.dumps({"id": "many", "name": "second"}),
        "2026-01-02T00:00:00+00:00",
        source,
    )
    first = store.read_current("Team", "many")
    assert first is not None
    store._insert_link_row("batchLink", "many", "z", first.lineage.valid_from)
    assert store.read_current_many("Team", ["many"]) == {"many": store.read_current("Team", "many")}
    assert store.read_current_many("Team", ["many"])["many"].payload["name"] == "many"
    assert store.links_from_many("batchLink", ["many"])["many"] == store.links_from(
        "batchLink", "many"
    )
    assert store.links_to_many("batchLink", ["z"])["z"] == store.links_to("batchLink", "z")
    assert store.links_from_many("batchLink", ["many"])["many"] == ["a", "z", "z", "é", "zero"]
    assert store.links_to_many("batchLink", ["z"])["z"] == ["many", "many", "one"]


@pytest.mark.parametrize(
    ("method", "step", "type_name"),
    [
        ("read_current_many", "_current_rows_many", "Team"),
        ("links_from_many", "_link_ids_from_many", "batchLink"),
        ("links_to_many", "_link_ids_to_many", "batchLink"),
    ],
)
def test_storage_receives_canonical_distinct_ids(
    store_factory: StoreFactory, monkeypatch, method, step, type_name
) -> None:
    store = store_factory(_registry())
    storage = Mock(wraps=getattr(store, step))
    monkeypatch.setattr(store, step, storage)
    getattr(store, method)(type_name, iter([_Id.MANY, "one", "many", "one"]))
    storage.assert_called_once_with(type_name, ["many", "one"])


def test_in_memory_batch_scans_each_storage_collection_once(monkeypatch) -> None:
    store = InMemoryStore(_registry())
    _seed(store)

    class CountingRows(list):
        scans = 0

        def __iter__(self):
            self.scans += 1
            return super().__iter__()

    objects = CountingRows(store._objects)
    links = CountingRows(store._links)
    monkeypatch.setattr(store, "_objects", objects)
    monkeypatch.setattr(store, "_links", links)
    store.read_current_many("Team", ["one", "many", "zero", "missing"])
    assert objects.scans == 1
    store.links_from_many("batchLink", ["one", "many", "zero", "missing"])
    assert links.scans == 1
    store.links_to_many("batchLink", ["z", "a", "zero", "missing"])
    assert links.scans == 2


@pytest.mark.parametrize("count", [1, 10, 2000])
@pytest.mark.parametrize(
    ("step", "type_name", "present"),
    [
        ("_current_rows_many", "Team", "many"),
        ("_link_ids_from_many", "batchLink", "many"),
        ("_link_ids_to_many", "batchLink", "z"),
    ],
)
def test_sql_batch_step_executes_one_statement(
    store_factory: StoreFactory, monkeypatch, count, step, type_name, present
) -> None:
    store = store_factory(_registry())
    if isinstance(store, InMemoryStore):
        pytest.skip("SQL statement contract applies to SQLite and Postgres")
    _seed(store)
    ids = [present, *(f"missing-{i}" for i in range(count - 1))]
    execute = Mock(wraps=_sql.execute)
    monkeypatch.setattr(_sql, "execute", execute)
    result = getattr(store, step)(type_name, ids)
    assert execute.call_count == 1
    if step == "_current_rows_many":
        assert list(result) == [present]
        assert result[present].payload == {"id": "many", "name": "many"}
    else:
        expected = ["a", "z", "é", "zero"] if present == "many" else ["many", "one"]
        assert result[present] == expected
        assert all(result[obj_id] == [] for obj_id in ids[1:])
