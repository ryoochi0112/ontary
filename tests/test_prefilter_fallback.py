"""Filtered reads contain driver failures and preserve the unfiltered contract."""

from __future__ import annotations

import os
import sqlite3
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime, timezone
from types import ModuleType

import pytest
from _fetch_counter import count_fallbacks, count_fetched

from ontary.meta import ObjectTypeDef, OntologyRegistry, PropertyDef
from ontary.query import GuardedQuery
from ontary.scope import ScopePolicy
from ontary.security import Consumer
from ontary.store import InMemoryStore, ObjectStore, Source
from ontary.store._core import StoreCore
from ontary.store._filter import RowFilter, WhereTerm, pushable

SRC = Source(source_system="fallback-test")
NOW = datetime(2026, 10, 8, tzinfo=timezone.utc)
READER = Consumer(actor_id="reader", role="Reader", scope_level="team",
                  scope_id="owned", kind="human")
FILTER = RowFilter(where=(WhereTerm("n", "int", "gte", 7),))
WHERE = {"n": {"gte": 7}}


@dataclass
class FallbackStore:
    backend: ModuleType
    store: StoreCore
    reference: InMemoryStore
    registry: OntologyRegistry
    dsn: str | None = None

    def query(self, store=None) -> GuardedQuery:
        policy = ScopePolicy(levels=["team"], min_n=1, unscoped_types={"Item"})
        policy.validate(self.registry)
        return GuardedQuery(self.store if store is None else store, self.registry, policy)


@pytest.fixture(params=["sqlite", "postgres"])
def seeded(request: pytest.FixtureRequest) -> Iterator[FallbackStore]:
    registry = OntologyRegistry()
    registry.register_object_type(ObjectTypeDef(
        api_name="Item", display_name="Item", description="Prefilter fallback", layer="L0",
        primary_key="id", properties=[
            PropertyDef(name="id", type="str"), PropertyDef(name="n", type="int"),
        ],
    ))
    registry.validate()
    schema = None
    store = None
    schema_dsn = None
    dsn = os.environ.get("ONTARY_TEST_POSTGRES_DSN")
    if request.param == "postgres":
        if not dsn:
            pytest.skip("ONTARY_TEST_POSTGRES_DSN is not set")
        import psycopg
        from psycopg.conninfo import make_conninfo

        from ontary.store import postgres as backend

        schema = "ontary_test_fallback_" + uuid.uuid4().hex
        with psycopg.connect(dsn, autocommit=True, options="-csearch_path=pg_catalog") as conn:
            conn.execute(f'CREATE SCHEMA "{schema}"')
        schema_dsn = make_conninfo(dsn, options=f"-csearch_path={schema}")
    else:
        from ontary.store import sqlite as backend
    try:
        store = (ObjectStore(registry) if schema is None
                 else backend.PostgresStore(registry, schema_dsn))
        reference = InMemoryStore(registry)
        for target in (store, reference):
            target.bind_clock(lambda: NOW)
            with target.transaction():
                for n in range(12):
                    target.insert("Item", {"id": str(n), "n": n}, SRC)
        yield FallbackStore(backend, store, reference, registry, schema_dsn)
    finally:
        if store is not None:
            store._conn.close()
        if schema is not None:
            with psycopg.connect(dsn, autocommit=True, options="-csearch_path=pg_catalog") as conn:
                conn.execute(f'DROP SCHEMA "{schema}" CASCADE')


@pytest.mark.parametrize("path", ["all", "page"])
def test_driver_failure_retries_same_read(seeded, monkeypatch, path) -> None:
    store = seeded.store
    if path == "page":
        after = store.read_page("Item", batch=2)[-1].key
        expected = store.read_page("Item", after_key=after, batch=3)
    else:
        expected = seeded.reference.read_all("Item")
    with count_fallbacks(store) as fallbacks:
        with monkeypatch.context() as patch:
            patch.setattr(seeded.backend, "compile_filter", lambda *_: ("invalid SQL !", []))
            if path == "all":
                result = store.read_all_filtered("Item", FILTER)
            else:
                result = store.read_page_filtered("Item", FILTER, after_key=after, batch=3)
        assert result == expected
        assert fallbacks == {"Item": 1}
        # The connection is still usable, including after a failed PG statement.
        assert store.read_all_filtered("Item", FILTER) == seeded.reference.read_all("Item")[7:]
        assert fallbacks == {"Item": 1}


@pytest.mark.parametrize("path", ["all", "page"])
def test_normal_prefilter_has_no_fallback_and_keeps_fetch_counter(seeded, path) -> None:
    with count_fallbacks(seeded.store) as fallbacks, count_fetched(seeded.store) as fetched:
        if path == "all":
            result = seeded.store.read_all_filtered("Item", FILTER)
            assert result == seeded.reference.read_all("Item")[7:]
            assert fetched == {"Item": 5}
        else:
            result = seeded.store.read_page_filtered("Item", FILTER, batch=2)
            assert [row.obj for row in result] == seeded.reference.read_all("Item")[7:9]
            assert fetched == {"Item": 2}
        assert fallbacks == {}


@pytest.mark.parametrize("limit", [None, 2])
def test_get_objects_continues_after_one_unfiltered_retry(seeded, monkeypatch, limit) -> None:
    query = seeded.query()
    expected = seeded.query(seeded.reference).get_objects(READER, "Item", WHERE, limit=limit)
    original = seeded.backend.compile_filter
    first = True

    def fail_once(*args):
        nonlocal first
        if first:
            first = False
            return "invalid SQL !", []
        return original(*args)

    monkeypatch.setattr(seeded.backend, "compile_filter", fail_once)
    with count_fallbacks(seeded.store) as fallbacks:
        result = query.get_objects(READER, "Item", WHERE, limit=limit)
        if limit is None:
            assert result == expected
        else:
            # The first batch contains no matching rows; it must keep walking.
            assert result.items == expected.items
            assert result.has_more == expected.has_more
            assert result.next_cursor is not None
        assert fallbacks == {"Item": 1}
        assert query.get_objects(READER, "Item", WHERE, limit=None) == (
            seeded.reference.read_all("Item")[7:]
        )
        assert fallbacks == {"Item": 1}


@pytest.mark.parametrize("oversized", [False, True], ids=["requested-batch", "larger-batch"])
def test_persistent_fallback_pages_terminate_and_resume(seeded, monkeypatch, oversized) -> None:
    batches = []
    original = seeded.store._page_rows

    def unfiltered(obj_type, after_row_id, batch):
        rows = original(obj_type, after_row_id, batch * 2 if oversized else batch)
        batches.append((batch, len(rows)))
        return rows

    monkeypatch.setattr(seeded.store, "_page_rows", unfiltered)
    monkeypatch.setattr(seeded.backend, "compile_filter", lambda *_: ("invalid SQL !", []))
    query = seeded.query()
    with count_fallbacks(seeded.store) as fallbacks:
        page = query.get_objects(READER, "Item", WHERE, limit=2)
        assert page.items == seeded.reference.read_all("Item")[7:9]
        assert page.has_more
        assert page.next_cursor is not None
        assert len(batches) == (2 if oversized else 4)
        assert all(batch == 3 for batch, _returned in batches)
        if oversized:
            assert all(returned > batch for batch, returned in batches)

        second = query.get_objects(READER, "Item", WHERE, limit=2, after=page.next_cursor)
        assert second.items == seeded.reference.read_all("Item")[9:11]
        assert second.has_more
        assert second.next_cursor is not None
        last = query.get_objects(READER, "Item", WHERE, limit=2, after=second.next_cursor)
        assert last.items == seeded.reference.read_all("Item")[11:]
        assert not last.has_more
        assert last.next_cursor is None
        assert fallbacks == {"Item": len(batches)}


@pytest.mark.parametrize("path", ["all", "page"])
def test_compiler_key_error_propagates_without_fallback(seeded, monkeypatch, path) -> None:
    error = KeyError("compiler bug")

    def broken(*_args):
        raise error

    with count_fallbacks(seeded.store) as fallbacks:
        with monkeypatch.context() as patch:
            patch.setattr(seeded.backend, "compile_filter", broken)
            with pytest.raises(KeyError) as raised:
                if path == "all":
                    seeded.store.read_all_filtered("Item", FILTER)
                else:
                    seeded.store.read_page_filtered("Item", FILTER, batch=2)
            assert raised.value is error
        assert seeded.store.read_all_filtered("Item", FILTER) == (
            seeded.reference.read_all("Item")[7:]
        )
        assert fallbacks == {}


@pytest.mark.parametrize("path", ["all", "page"])
def test_compiler_error_propagates_without_fallback(seeded, monkeypatch, path) -> None:
    error = ValueError("compiler bug")

    def broken(*_args):
        raise error

    monkeypatch.setattr(seeded.backend, "compile_filter", broken)
    with count_fallbacks(seeded.store) as fallbacks:
        with pytest.raises(ValueError) as raised:
            if path == "all":
                seeded.store.read_all_filtered("Item", FILTER)
            else:
                seeded.store.read_page_filtered("Item", FILTER, batch=2)
        assert raised.value is error
        assert fallbacks == {}


@pytest.mark.parametrize("path", ["all", "page"])
def test_failed_unfiltered_retry_propagates(seeded, monkeypatch, path) -> None:
    error_type = sqlite3.OperationalError if seeded.dsn is None else seeded.backend._psycopg().Error
    error = error_type("unfiltered read failed")

    def broken(*_args):
        raise error

    monkeypatch.setattr(seeded.backend, "compile_filter", lambda *_: ("invalid SQL !", []))
    monkeypatch.setattr(seeded.store, "_all_rows" if path == "all" else "_page_rows", broken)
    with count_fallbacks(seeded.store) as fallbacks:
        with pytest.raises(error_type) as raised:
            if path == "all":
                seeded.store.read_all_filtered("Item", FILTER)
            else:
                seeded.store.read_page_filtered("Item", FILTER, batch=2)
        assert raised.value is error
        assert fallbacks == {"Item": 1}


@pytest.mark.parametrize("error_type", [ValueError, OverflowError])
@pytest.mark.parametrize("path", ["all", "page"])
def test_bind_error_families_are_backend_specific(seeded, monkeypatch, error_type, path) -> None:
    error = error_type("binding failed")
    sql = seeded.backend._sql
    dialect = "sqlite" if seeded.dsn is None else "postgres"
    fragment, _params = seeded.backend.compile_filter(FILTER, dialect, seeded.store.bind_domain)
    template = (seeded.backend.OBJECT_FILTERED_ALL_SELECT_TEMPLATE if path == "all"
                else seeded.backend.OBJECT_FILTERED_PAGE_SELECT_TEMPLATE)
    filtered_sql = sql.render(template.replace("{filter}", fragment), dialect)
    execute = sql.execute

    def broken(conn, statement, params=(), *, dialect):
        # Fail only the filtered statement, inside the prefilter's guarded read.
        if conn is seeded.store._conn and statement == filtered_sql:
            raise error
        return execute(conn, statement, params, dialect=dialect)

    monkeypatch.setattr(sql, "execute", broken)
    with count_fallbacks(seeded.store) as fallbacks:
        def read():
            if path == "all":
                return seeded.store.read_all_filtered("Item", FILTER)
            return seeded.store.read_page_filtered("Item", FILTER, batch=2)

        if seeded.dsn is None:
            result = read()
            expected = seeded.reference.read_all("Item")
            assert (result if path == "all" else [row.obj for row in result]) == (
                expected if path == "all" else expected[:2]
            )
            assert fallbacks == {"Item": 1}
        else:
            with pytest.raises(error_type) as raised:
                read()
            assert raised.value is error
            assert fallbacks == {}


@pytest.mark.parametrize("path", ["all", "page"])
def test_transaction_keeps_earlier_insert_and_advisory_lock(seeded, monkeypatch, path) -> None:
    if seeded.dsn is None:
        pytest.skip("Postgres transaction/advisory-lock regression")
    from ontary.store.postgres import PostgresStore

    store = seeded.store

    def lock_count():
        return store._conn.execute(
            "SELECT count(*) FROM pg_locks WHERE pid = pg_backend_pid() "
            "AND locktype = 'advisory' AND granted"
        ).fetchone()[0]

    with count_fallbacks(store) as fallbacks:
        with store.transaction():
            store.insert("Item", {"id": "12", "n": 12}, SRC)
            assert lock_count() == 1
            with monkeypatch.context() as patch:
                patch.setattr(seeded.backend, "compile_filter", lambda *_: ("invalid SQL !", []))
                if path == "all":
                    rows = store.read_all_filtered("Item", FILTER)
                else:
                    rows = [row.obj for row in store.read_page_filtered("Item", FILTER, batch=20)]
            assert rows == store.read_all("Item")
            assert rows[-1].payload == {"id": "12", "n": 12}
            assert store.in_transaction
            assert lock_count() == 1
            assert store.read_all_filtered("Item", FILTER) == rows[7:]
            assert fallbacks == {"Item": 1}
        assert not store.in_transaction
        fresh = PostgresStore(seeded.registry, seeded.dsn)
        try:
            assert fresh.read_current("Item", "12").payload == {"id": "12", "n": 12}
        finally:
            fresh._conn.close()


@pytest.mark.parametrize("limit", [None, 2])
def test_sqlite_oversize_in_array_retries_unfiltered(seeded, limit) -> None:
    if seeded.dsn is not None:
        pytest.skip("SQLite connection length limit")
    values = list(range(3000))
    assert all(pushable(value, seeded.store.bind_domain) for value in values)
    where = {"n": {"in": values}}
    expected = seeded.query(seeded.reference).get_objects(READER, "Item", where, limit=limit)
    conn = seeded.store._conn
    previous = conn.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, 4096)
    try:
        with count_fallbacks(seeded.store) as fallbacks:
            result = seeded.query().get_objects(READER, "Item", where, limit=limit)
            if limit is None:
                assert result == expected
            else:
                assert result.items == expected.items
                assert result.has_more == expected.has_more
            assert fallbacks == {"Item": 1}
            assert seeded.store.read_all_filtered("Item", FILTER) == (
                seeded.reference.read_all("Item")[7:]
            )
            assert fallbacks == {"Item": 1}
    finally:
        conn.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, previous)
